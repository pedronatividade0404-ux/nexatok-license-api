# NexaTok License API

Use the GitHub private repository as the license database.

1. Create the private repository.
2. Put `basic.txt`, `pro.txt`, `ultimate.txt`, and `prices.txt` in it.
3. Create a fine-grained GitHub token limited to that repository.
4. Configure the environment variables from `.env.example`.
5. Run with `uvicorn main:app --host 0.0.0.0 --port 8000`.
6. Put the API behind HTTPS.
