"""Punto de entrada WSGI:  waitress-serve --call wsgi:app   |   gunicorn wsgi:app"""
from scrapbot import create_app

app = create_app()
