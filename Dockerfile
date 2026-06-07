FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .
EXPOSE 8088
CMD ["abenlux", "gateway", "--port", "8088"]
