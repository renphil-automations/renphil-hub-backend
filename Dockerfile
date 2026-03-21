FROM python:3.12-slim

# Allow Google to return extra scopes without oauthlib raising
ENV OAUTHLIB_RELAX_TOKEN_SCOPE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
