FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY redhat_kcs_mcp.py .
EXPOSE 8080
CMD ["python", "redhat_kcs_mcp.py"]
