# AppNeta → Google Chat with Gemini AI Triage

A Cloud Run function that turns AppNeta alarms into Google Chat cards with an AI-generated root cause and two recommended actions. Raise and clear events for the same alarm share one Chat thread.

Companion code for the blog post [Send AppNeta Network Alerts to Google Chat with Gemini AI Summaries](<BLOG_URL>).

> [!IMPORTANT]
> This is a personal sample provided "as is", without warranty of any kind. It is not an official product, is not supported and may not be updated. Review and test it before any production use. Google Cloud costs are your responsibility.

## Configuration

| Setting | Required | Description |
|---|---|---|
| `GCHAT_WEBHOOK_URL` | Yes (secret) | Google Chat space → Apps & integrations → Webhooks |
| `WEBHOOK_SECRET` | Yes (secret) | A random string (`openssl rand -hex 32`). AppNeta sends it in the `X-AppNeta-Token` header |
| `GCP_PROJECT` | Yes | Your Google Cloud project ID |
| `GCP_REGION` | No | Vertex AI location (default `global`) |
| `GEMINI_MODEL` | No | Default `gemini-2.5-flash`. Gemini 2.5 is being retired on Vertex AI; set a current Flash model from [model versions](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions) |
| `GRAFANA_URL`, `ITSM_URL` | No | Optional card links; hidden when not set |

## Deploy

Replace `<PROJECT_ID>` and `<REGION>`, then run from the repository folder:

```bash
# 1. Enable APIs
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com secretmanager.googleapis.com --project <PROJECT_ID>

# 2. Store secrets
printf '%s' '<GCHAT_WEBHOOK_URL>' | gcloud secrets create gchat-webhook-url --data-file=- --project <PROJECT_ID>
printf '%s' '<WEBHOOK_SECRET>'    | gcloud secrets create appneta-token     --data-file=- --project <PROJECT_ID>

# 3. Service account with Vertex AI User + access to the two secrets
gcloud iam service-accounts create appneta-gchat-bridge --project <PROJECT_ID>
SA=appneta-gchat-bridge@<PROJECT_ID>.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding <PROJECT_ID> --member="serviceAccount:$SA" --role="roles/aiplatform.user"
for s in gchat-webhook-url appneta-token; do
  gcloud secrets add-iam-policy-binding $s --project <PROJECT_ID> \
    --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
done

# 4. Deploy (unauthenticated: AppNeta can't send Google IAM tokens; the shared secret protects the endpoint)
gcloud run deploy appneta-gchat-bridge --source . --function webhook_handler --base-image python312 \
  --region <REGION> --project <PROJECT_ID> --service-account "$SA" --allow-unauthenticated \
  --set-env-vars GCP_PROJECT=<PROJECT_ID> \
  --set-secrets GCHAT_WEBHOOK_URL=gchat-webhook-url:latest,WEBHOOK_SECRET=appneta-token:latest
```

Then, in AppNeta, open the gear icon → **Explore API** (Swagger, `/v4`) and create an alarm connector using [`appneta-connector.example.json`](appneta-connector.example.json). Replace the org ID, the function URL and the secret. Check the field names against your tenant's Swagger schema.

## Test

```bash
curl -X POST "<FUNCTION_URL>" -H "Content-Type: application/json" -H "X-AppNeta-Token: <WEBHOOK_SECRET>" \
  -d '{"alarmId":"test-1","state":"RAISED","alarmSeverity":"CRITICAL","rule":"Test alarm","itemName":"test-path","target":"example.com"}'
```

Send it again with `"state":"CLEARED"`: the second card should appear in the same thread. A wrong token returns `401`.

## License

[MIT](LICENSE)
