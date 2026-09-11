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

        # On mémorise à qui appartient cette session précise.
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

        # On garde le statut et le transaction_id à jour sur NOTRE session,
        # sans jamais toucher aux autres sessions en attente.
        db.collection("sessions").document(session_id).set({
            "statut": result.get("status"),
            "transaction_id": result.get("transaction"),
        }, merge=True)

        return jsonify(result), 200

    return jsonify({"error": r.text}), r.status_code


# --- Vérifier un paiement précis ---
@app.route("/verifier/<transaction_id>", methods=["GET"])
def verifier(transaction_id):
    r = requests.get(f"{SASPAY_BASE_URL}/payments/{transaction_id}/verify/", headers=HEADERS)

    if r.status_code != 200:
        return jsonify({"error": r.text}), r.status_code

    result = r.json().get("data", r.json())
    statut = result.get("status")

    # On retrouve UNIQUEMENT la session liée à cette transaction précise
    # (et non plus toutes les sessions PENDING de tout le monde).
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
        montant = doc.get("montant")

        # Historique complet de la transaction, quel que soit le résultat
        # (succès, échec, annulation...).
        db.collection("transactions").document(transaction_id).set({
            "user_id": user_id,
            "montant": montant,
            "statut": statut,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)

        # Mise à jour de la session correspondante seulement.
        db.collection("sessions").document(session_doc.id).update({"statut": statut})

        if statut == "SUCCESS":
            db.collection("paiements").document(user_id).set({
                "montant": montant,
                "statut": "success",
                "dernier_transaction_id": transaction_id,
            }, merge=True)
        else:
            # Paiement refusé, échoué ou annulé : on le trace aussi,
            # au lieu de ne rien enregistrer.
            db.collection("paiements").document(user_id).set({
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

    if utilisateur:
        db.collection("paiements").document(utilisateur).set({
            "montant": montant,
            "statut": statut,
        }, merge=True)

    return jsonify({"received": True}), 200


if __name__ == "__main__":
    app.run()
