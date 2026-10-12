FROM python:3.12-slim
RUN pip install --no-cache-dir notebooklm-mcp-cli==0.15.5
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
