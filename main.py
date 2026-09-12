import json
import os
import requests
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore

# Initialisation Firebase sécurisée via les variables d'environnement de Render
firebase_config = json.loads(os.environ.get("FIREBASE_CONFIG_JSON"))
cred = credentials.Certificate(firebase_config)
firebase_admin.initialize_app(cred)
db = firestore.client()

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


# --- Créer une session de paiement ---
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


# --- Relire une session de checkout (utilisé par le suivi automatique Flutter) ---
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
    """
    Crédite le solde de l'utilisateur UNE SEULE FOIS pour cette transaction,
    même si cette fonction est appelée plusieurs fois (double clic sur
    "Vérifier", minuteur qui repasse dessus, appli relancée, etc.).

    On utilise une transaction Firestore : le document
    recharges/{transaction_id} sert de verrou. S'il existe déjà, on ne fait
    rien de plus.
    """
    recharge_ref = db.collection("recharges").document(transaction_id)

    @firestore.transactional
    def _run(transaction):
        snapshot = recharge_ref.get(transaction=transaction)
        if snapshot.exists:
            # Déjà traité précédemment : on ne recrédite rien.
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


# --- Vérifier un paiement précis ---
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

        # Crédit du solde (une seule fois, quel que soit le nombre d'appels).
        _crediter_si_pas_deja_fait(transaction_id, user_id, montant, devise, statut)

        db.collection("paiements").document(user_id).set({
            "montant": montant if statut == "SUCCESS" else None,
            "statut": statut.lower() if statut else "unknown",
            "dernier_transaction_id": transaction_id,
        }, merge=True)

    return jsonify(result), 200


# --- Webhook SasPay (si dispo) ---
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


if __name__ == "__main__":
    app.run()
