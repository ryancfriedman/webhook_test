import logging
from flask import Flask, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

@app.get("/health")
def health():
    return "ok"

@app.post("/webhook")
def webhook():
    logging.info("HEADERS: %s", dict(request.headers))
    logging.info("BODY: %s", request.get_data().decode("utf-8", "replace"))
    return "", 200
