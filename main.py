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
        # On mémorise à qui appartient cette session
        db.collection("sessions").document(result["id"]).set({
            "user_id": user_id,
            "montant": montant,
            "statut": result.get("status", "PENDING"),
        })
        return jsonify(result), 200

    return jsonify({"error": r.text}), r.status_code

# --- Vérifier un paiement ---
@app.route("/verifier/<transaction_id>", methods=["GET"])
def verifier(transaction_id):
    r = requests.get(f"{SASPAY_BASE_URL}/payments/{transaction_id}/verify/", headers=HEADERS)

    if r.status_code == 200:
        result = r.json().get("data", r.json())
        statut = result.get("status")

        if statut == "SUCCESS":
            # Retrouver l'utilisateur lié à cette transaction
            sessions = db.collection("sessions").where("statut", "==", "PENDING").stream()
            for s in sessions:
                doc = s.to_dict()
                db.collection("paiements").document(doc["user_id"]).set({
                    "montant": doc["montant"],
                    "statut": "success",
                }, merge=True)
                db.collection("sessions").document(s.id).update({"statut": "SUCCESS"})

        return jsonify(result), 200

    return jsonify({"error": r.text}), r.status_code

# --- Webhook SasPay (si dispo) ---
@app.route("/webhook/saspay", methods=["POST"])
def saspay_webhook():
    data = request.json
    montant = data.get("amount")
    utilisateur = data.get("user_id")
    statut = data.get("status")

    if statut == "success":
        db.collection("paiements").document(utilisateur).set({
            "montant": montant,
            "statut": statut
        }, merge=True)

    return jsonify({"received": True}), 200

if __name__ == "__main__":
    app.run()
