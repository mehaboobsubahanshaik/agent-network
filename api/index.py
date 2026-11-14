"""Vercel entry point. Vercel turns this file into one serverless function that serves /api/*.

All the real code lives in the `backend` package; this file only exposes its FastAPI `app`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.main import app  # noqa: E402,F401
