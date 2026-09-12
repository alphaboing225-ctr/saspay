import json
import os
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
import cloudinary
import cloudinary.uploader

# Initialisation Firebase sécurisée via les variables d'environnement de Render
firebase_config = json.loads(os.environ.get("FIREBASE_CONFIG_JSON"))
cred = credentials.Certificate(firebase_config)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Cloudinary — nécessaire pour supprimer les images des pubs expirées.
# Ajoutez CLOUDINARY_API_KEY et CLOUDINARY_API_SECRET dans les variables
# d'environnement Render (visibles dans votre tableau de bord Cloudinary).
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "csgiimmi"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
)

app = Flask(__name__)

SASPAY_SECRET_KEY = os.environ.get("SASPAY_SECRET_KEY")
SASPAY_BASE_URL = "https://api.saspay.me/api/v1"
HEADERS = {
    "Authorization": f"Bearer {SASPAY_SECRET_KEY}",
    "Content-Type": "application/json",
}


@app.route("/")
def home():
    return "OK"


# ====================================================================
# 💳 RECHARGE (SasPay)
# ====================================================================

@app.route("/creer-session", methods=["POST"])
def creer_session():
    data = request.json
    montant = data.get("amount")
    email = data.get("email")
    nom = data.get("name")
    user_id = data.get("user_id")

    payload = {
        "amount": f"{montant:.2f}",
        "currency": "XOF",
        "description": "Recharge du solde publicitaire",
        "customer_email": email,
        "customer_name": nom,
    }
    r = requests.post(f"{SASPAY_BASE_URL}/checkout-sessions/", json=payload, headers=HEADERS)

    if r.status_code in (200, 201):
        result = r.json().get("data", r.json())

        db.collection("sessions").document(result["id"]).set({
            "user_id": user_id,
            "montant": montant,
            "email": email,
            "nom": nom,
            "statut": result.get("status", "PENDING"),
            "transaction_id": result.get("transaction"),
            "created_at": firestore.SERVER_TIMESTAMP,
        })
        return jsonify(result), 200

    return jsonify({"error": r.text}), r.status_code


@app.route("/checkout-sessions/<session_id>/", methods=["GET"])
def relire_session(session_id):
    r = requests.get(f"{SASPAY_BASE_URL}/checkout-sessions/{session_id}/", headers=HEADERS)

    if r.status_code == 200:
        result = r.json().get("data", r.json())

        db.collection("sessions").document(session_id).set({
            "statut": result.get("status"),
            "transaction_id": result.get("transaction"),
        }, merge=True)

        return jsonify(result), 200

    return jsonify({"error": r.text}), r.status_code


def _crediter_si_pas_deja_fait(transaction_id, user_id, montant, devise, statut):
    """Crédite le solde UNE SEULE FOIS par transaction (idempotent)."""
    recharge_ref = db.collection("recharges").document(transaction_id)

    @firestore.transactional
    def _run(transaction):
        snapshot = recharge_ref.get(transaction=transaction)
        if snapshot.exists:
            return False

        transaction.set(recharge_ref, {
            "uid": user_id,
            "montant": montant,
            "devise": devise,
            "statut": statut,
            "transactionId": transaction_id,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })

        if statut == "SUCCESS":
            user_ref = db.collection("users").document(user_id)
            transaction.set(user_ref, {
                "pub_solde": firestore.Increment(montant),
            }, merge=True)

        return True

    return _run(db.transaction())


@app.route("/verifier/<transaction_id>", methods=["GET"])
def verifier(transaction_id):
    r = requests.get(f"{SASPAY_BASE_URL}/payments/{transaction_id}/verify/", headers=HEADERS)

    if r.status_code != 200:
        return jsonify({"error": r.text}), r.status_code

    result = r.json().get("data", r.json())
    statut = result.get("status")
    montant_net = result.get("net_amount")
    devise = result.get("currency", "XOF")

    sessions = (
        db.collection("sessions")
        .where("transaction_id", "==", transaction_id)
        .limit(1)
        .stream()
    )
    session_doc = next(sessions, None)

    if session_doc is not None:
        doc = session_doc.to_dict()
        user_id = doc.get("user_id")
        montant = float(montant_net) if montant_net is not None else doc.get("montant")

        db.collection("sessions").document(session_doc.id).update({"statut": statut})

        db.collection("transactions").document(transaction_id).set({
            "user_id": user_id,
            "montant": montant,
            "statut": statut,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)

        _crediter_si_pas_deja_fait(transaction_id, user_id, montant, devise, statut)

        db.collection("paiements").document(user_id).set({
            "montant": montant if statut == "SUCCESS" else None,
            "statut": statut.lower() if statut else "unknown",
            "dernier_transaction_id": transaction_id,
        }, merge=True)

    return jsonify(result), 200


@app.route("/webhook/saspay", methods=["POST"])
def saspay_webhook():
    data = request.json
    montant = data.get("amount")
    utilisateur = data.get("user_id")
    statut = data.get("status")
    transaction_id = data.get("transaction_id") or data.get("id")

    if utilisateur and transaction_id:
        _crediter_si_pas_deja_fait(
            transaction_id, utilisateur, float(montant) if montant else 0,
            data.get("currency", "XOF"), statut.upper() if statut else "UNKNOWN",
        )

    return jsonify({"received": True}), 200


# ====================================================================
# 📢 PUBS PAYANTES — création, renouvellement, expiration
# ====================================================================

@app.route("/creer-pub", methods=["POST"])
def creer_pub():
    data = request.json
    user_id = data.get("user_id")
    prix = data.get("prix")
    duree_jours = data.get("duree_jours")
    images = data.get("images", [])  # [{"url": "...", "publicId": "..."}]
    lien_redirection = data.get("lien_redirection")

    if not user_id or prix is None or duree_jours is None:
        return jsonify({"error": "parametres_manquants"}), 400

    user_ref = db.collection("users").document(user_id)
    pub_ref = db.collection("pubs").document()

    @firestore.transactional
    def _run(transaction):
        snapshot = user_ref.get(transaction=transaction)
        solde_actuel = 0
        if snapshot.exists:
            solde_actuel = (snapshot.to_dict() or {}).get("pub_solde", 0) or 0

        if solde_actuel < prix:
            return {"ok": False, "solde_actuel": solde_actuel}

        transaction.update(user_ref, {"pub_solde": firestore.Increment(-prix)})

        maintenant = datetime.now(timezone.utc)
        date_fin = maintenant + timedelta(days=duree_jours)

        transaction.set(pub_ref, {
            "uid": user_id,
            "images": images,
            "lienRedirection": lien_redirection,
            "statut": "active",
            "prixPaye": prix,
            "dureeJours": duree_jours,
            "dateDebut": maintenant,
            "dateFin": date_fin,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        return {"ok": True}

    resultat = _run(db.transaction())

    if not resultat["ok"]:
        return jsonify({
            "error": "solde_insuffisant",
            "solde_actuel": resultat["solde_actuel"],
        }), 402

    return jsonify({"success": True, "pub_id": pub_ref.id}), 200


@app.route("/renouveler-pub", methods=["POST"])
def renouveler_pub():
    data = request.json
    user_id = data.get("user_id")
    pub_id = data.get("pub_id")
    prix = data.get("prix")
    duree_jours = data.get("duree_jours")

    if not user_id or not pub_id or prix is None or duree_jours is None:
        return jsonify({"error": "parametres_manquants"}), 400

    user_ref = db.collection("users").document(user_id)
    pub_ref = db.collection("pubs").document(pub_id)

    @firestore.transactional
    def _run(transaction):
        user_snap = user_ref.get(transaction=transaction)
        pub_snap = pub_ref.get(transaction=transaction)

        if not pub_snap.exists or pub_snap.to_dict().get("uid") != user_id:
            return {"ok": False, "raison": "pub_introuvable"}

        solde_actuel = (user_snap.to_dict() or {}).get("pub_solde", 0) or 0
        if solde_actuel < prix:
            return {"ok": False, "raison": "solde_insuffisant", "solde_actuel": solde_actuel}

        transaction.update(user_ref, {"pub_solde": firestore.Increment(-prix)})

        maintenant = datetime.now(timezone.utc)
        date_fin = maintenant + timedelta(days=duree_jours)

        transaction.update(pub_ref, {
            "statut": "active",
            "prixPaye": prix,
            "dureeJours": duree_jours,
            "dateDebut": maintenant,
            "dateFin": date_fin,
            "dateLimiteRenouvellement": firestore.DELETE_FIELD,
        })
        return {"ok": True}

    resultat = _run(db.transaction())

    if not resultat["ok"]:
        code = 402 if resultat.get("raison") == "solde_insuffisant" else 404
        return jsonify(resultat), code

    return jsonify({"success": True}), 200


def _supprimer_image_cloudinary(public_id):
    try:
        cloudinary.uploader.destroy(public_id)
    except Exception as e:
        print(f"Erreur suppression Cloudinary ({public_id}) : {e}")


@app.route("/verifier-pubs", methods=["GET", "POST"])
def verifier_pubs():
    """
    À appeler périodiquement (ex. Render Cron Job une fois par jour, ou à
    défaut manuellement / à l'ouverture de l'appli) :
      1. Les pubs actives dont dateFin est dépassée passent en
         "en_attente_renouvellement" avec 7 jours de délai.
      2. Les pubs en attente de renouvellement dont le délai de 7 jours
         est dépassé sont supprimées définitivement (Firestore + images
         Cloudinary).
    """
    maintenant = datetime.now(timezone.utc)

    pubs_expirees = (
        db.collection("pubs")
        .where("statut", "==", "active")
        .where("dateFin", "<=", maintenant)
        .stream()
    )
    nb_passees_en_attente = 0
    for doc in pubs_expirees:
        date_limite = maintenant + timedelta(days=7)
        doc.reference.update({
            "statut": "en_attente_renouvellement",
            "dateLimiteRenouvellement": date_limite,
        })
        nb_passees_en_attente += 1

    pubs_a_supprimer = (
        db.collection("pubs")
        .where("statut", "==", "en_attente_renouvellement")
        .where("dateLimiteRenouvellement", "<=", maintenant)
        .stream()
    )
    nb_supprimees = 0
    for doc in pubs_a_supprimer:
        data = doc.to_dict()
        for image in data.get("images", []):
            public_id = image.get("publicId") if isinstance(image, dict) else None
            if public_id:
                _supprimer_image_cloudinary(public_id)
        doc.reference.delete()
        nb_supprimees += 1

    return jsonify({
        "verifie_le": maintenant.isoformat(),
        "passees_en_attente": nb_passees_en_attente,
        "supprimees": nb_supprimees,
    }), 200


if __name__ == "__main__":
    app.run()
