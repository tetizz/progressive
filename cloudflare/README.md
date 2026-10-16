# Cloudflare Pages

The public Pages site calls `/api/health`, `/api/prefix` and `/api/analyze` on its own origin. The Pages worker forwards only those methods and paths to the existing anonymous Render API. GitHub Pages continues to use the configured Render origin directly.

The worker streams JSON responses, preserves upstream status and body, and forwards caller cancellation. It introduces no response-size cap or additional transport timeout. The request-size limit matches the backend's 256 KiB limit. Incoming request cancellation requires the `enable_request_signal` compatibility flag in `wrangler.toml`. Transport cancellation does not prove that remote engine computation stopped.

Full hosted Analyze requests without an explicit receipt deadline get the same 1.5-second delivery grace already used by Play. Search time, work budgets, explicit caller deadlines and local WASM deadlines are unchanged. Incomplete searches retain their actual depth, timeout and proof labels.

Keep these Cloudflare files outside `web/static`: GitHub Pages uses that directory as its static artifact. Assemble a separate Cloudflare artifact from the existing certified static tree, then overlay `_worker.js` and `_routes.json` from this directory. Deploy the worker and app together.

For a fresh release artifact, run the existing browser-release validation gates documented in `.github/workflows/pages.yml`, then build into a new directory:

```powershell
python scripts/build_pages_site.py --source src/scottish_progressive/web/static --output _cloudflare_site --version RELEASE_COMMIT
Copy-Item cloudflare/_worker.js _cloudflare_site/_worker.js
Copy-Item cloudflare/_routes.json _cloudflare_site/_routes.json
Set-Location cloudflare
wrangler pages deploy ../_cloudflare_site --project-name tetizz-progressive --branch repair-analysis-api
```

Replace `RELEASE_COMMIT` with the actual commit. Validate the complete preview, API responses, certified local Play, full analysis and PV controls before publishing the same artifact with `--branch main`. Preserve the certified engine files and release receipts; this frontend repair does not require rebuilding the engine. Compare the complete artifact map before publishing.
