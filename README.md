# 📄 FixMyPaper — Research Paper Formatter Checker

A tool that checks research papers (PDFs) for formatting errors and style violations. Upload a PDF, select a format, and get a detailed report of issues found.

Built with **Flask** (backend) + **Next.js** (frontend), containerised with Docker.

---

## Features

- Upload any research paper PDF
- Select a target format/style guide
- Detects formatting issues across sections (tables, figures, references, headings, etc.)
- Clean web UI with downloadable results

---

## Requirements

- [Docker Desktop](https://docs.docker.com/get-docker/) (includes Docker Compose)
- That's it — no Python or Node.js needed locally

---

## Getting Started

1. **Clone the repo**

   ```bash
   git clone https://github.com/your-username/research-paper-checker.git
   cd research-paper-checker
   ```

2. **Build and start**

   ```bash
   docker compose up --build
   ```

   The first build downloads base images and installs all dependencies — this can take **10–30 minutes** depending on your internet speed. Subsequent starts are fast.

3. **Open the app**

   | Service | URL |
   |---|---|
   | Frontend (use this) | http://localhost:3000 |
   | Backend API | http://localhost:5001 |

4. **Stop the app**

   ```bash
   docker compose down
   ```

---

## Project Structure

```
research-paper-checker/
├── app.py                  # Flask entry point
├── pdf_processor.py        # PDF parsing and check logic
├── requirements.txt        # Python dependencies
├── Dockerfile.backend      # Backend container
├── docker-compose.yml      # Orchestration
└── frontend/
    ├── app/                # Next.js app router
    ├── components/         # UI components
    ├── Dockerfile          # Frontend container
    └── package.json
```

---

## Development

### Rebuilding after code changes

```bash
docker compose build
docker compose up
```

### Rebuilding after changing dependencies

```bash
# Python (requirements.txt changed)
docker compose build --no-cache backend

# Node (package.json changed)
docker compose build --no-cache frontend
```

### Viewing logs

```bash
docker compose logs -f backend
docker compose logs -f frontend
```

---

## Troubleshooting

**Frontend shows "backend unavailable"**  
Make sure both containers are running: `docker compose ps`. Both should show `running`, not `restarting`.

**Build fails with network errors**  
Add DNS settings to Docker Desktop: Settings → Docker Engine → add `"dns": ["8.8.8.8", "8.8.4.4"]`, then restart Docker.

**PDF processing is very slow**  
This is expected for large papers — the ML models take time. The timeout is set to 15 minutes by default.

---

## College Server Deploy (Prebuilt Images)

Use the prebuilt deployment flow so the server only pulls and runs images (no source build on server).

See [docs/DEPLOY_PREBUILT.md](docs/DEPLOY_PREBUILT.md) for exact publish and deploy commands.

---

## License

MIT