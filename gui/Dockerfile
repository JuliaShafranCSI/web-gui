FROM python:3.11-slim
RUN apt-get update && apt-get install -y libgl1 && rm -rf /var/lib/apt/lists/*
WORKDIR /home/workspace
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x start.sh
CMD ["sh", "start.sh"]
