import os
import requests
from fastapi import FastAPI, HTTPException

app = FastAPI()

# Récupération des identifiants stockés dans les variables d'environnement Render
SASAPAY_CLIENT_ID = os.getenv("5ieAjfdagsSQT8OWz2RW3ZaTYEsiNgKklNP7V20f")
SASAPAY_CLIENT_SECRET = os.getenv("BAPBdmuS6Ye9SpD7iSCgcYmtvEtMwyZRBYCmXix23G8lRA6uppkWSVDpY5JxqMAuthvpBiTEMh8jUhfJRuJGiiqsElm4LGpmYX0GaUkLwlMli1qJ3oYQxdATpBuK56Q7")

# URL de l'authentification SasaPay (exemple pour la Sandbox / test)
AUTH_URL = "https://sandbox.sasapay.app/oauth/v1/generate?grant_type=client_credentials"

@app.get("/")
def read_root():
    return {"message": "Cloud Function FastAPI avec SasaPay active !"}

@app.get("/get-token")
def get_sasapay_token():
    if not SASAPAY_CLIENT_ID or not SASAPAY_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Les clés SasaPay ne sont pas configurées.")
    
    # Appel pour générer le jeton d'accès
    response = requests.get(
        AUTH_URL,
        auth=(SASAPAY_CLIENT_ID, SASAPAY_CLIENT_SECRET)
    )
    
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Échec de l'authentification auprès de SasaPay")
        
    return response.json()