FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY jobgen.py webhook_receiver.py publish.py hub_page.py townpage.py vertical.py wsgi.py ./
COPY verticals/ ./verticals/
COPY clients/ ./clients/
# engine/ carries the config + pack JSON schemas the control plane serves to
# the onboarding wizard (GET /admin/packs). Selective COPY bit us once before
# the cutover (vertical.py) — keep this list honest when adding directories.
COPY engine/ ./engine/

ENV PYTHONUNBUFFERED=1
EXPOSE 8787

# One worker — batch state is in-process. See wsgi.py.
CMD gunicorn wsgi:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT:-8787}
