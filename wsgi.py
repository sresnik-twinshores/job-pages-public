"""Gunicorn entrypoint.

IMPORTANT: run with exactly ONE worker.

Pending photo batches live in an in-memory dict, and the sweeper that fires them is a
thread inside the process. With 2+ workers, GHL's webhooks for a single job would be
load-balanced across processes, each holding a partial batch — so one job would become
several half-empty pages. One worker with threads handles this load fine; if it ever
needs to scale, the batch state has to move to Redis or the database first.

    gunicorn wsgi:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT
"""
from webhook_receiver import app, boot

boot()
