FROM python:3.11-slim

# РЎРёСЃС‚РµРјРЅС– Р·Р°Р»РµР¶РЅРѕСЃС‚С–
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# РЎРїРѕС‡Р°С‚РєСѓ РєРѕРїС–СЋС”РјРѕ requirements РґР»СЏ РєРµС€СѓРІР°РЅРЅСЏ С€Р°СЂС–РІ
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# РљРѕРїС–СЋС”РјРѕ РєРѕРґ Р±РѕС‚Р°
COPY bot.py .

# РџР°РїРєР° РґР»СЏ Р·Р±РµСЂРµР¶РµРЅРЅСЏ seen_ids.json (Р±СѓРґРµ РјРѕРЅС‚СѓРІР°С‚РёСЃСЊ СЏРє volume)
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
