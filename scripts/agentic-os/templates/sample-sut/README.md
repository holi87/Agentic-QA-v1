# Sample SUT scaffold

Spun up by `agentic-os init --sample-sut`. Two public-image services
brought up by `docker-compose up`:

| Service       | URL                          | Role                              |
|---------------|------------------------------|-----------------------------------|
| sample-web    | <http://localhost:8080>      | static pages for the UI crawler   |
| sample-api    | <http://localhost:8081>      | httpbin for the API generator     |

After `init --sample-sut`, `config/agentic-os.yml` is rewritten with:

```yaml
sut:
  compose_file: sample-sut/docker-compose.yml
  web:
    enabled: true
    url: http://localhost:8080
  api:
    enabled: true
    openapi:
      sources: [sample-sut/openapi.yaml]
```

Bring it up alongside the orchestrator with `agentic-os up`. Stop both
with `agentic-os down --stop-sut`.
