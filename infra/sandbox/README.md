# CodeReferee Sandbox Pool

This is a fixed-size local sandbox pool for repository validation.
AI Core calls only the gateway URL, while Docker Compose keeps three sandbox workers running.

```text
AI worker -> http://<sandbox-host>:8100/repositories/validate
          -> nginx gateway
          -> sandbox-1 | sandbox-2 | sandbox-3
```

## Start

```bash
docker compose up -d --build sandbox-gateway sandbox-1 sandbox-2 sandbox-3
```

Health check:

```bash
curl http://localhost:8100/health
```

Validate a repository directly:

```bash
curl -X POST http://localhost:8100/repositories/validate \
  -H 'Content-Type: application/json' \
  -d '{"repositoryUrl":"https://github.com/CodeReferee-Team/codereferee-AI","branch":"main"}'
```

AI Core configuration:

```env
SANDBOX_BASE_URL=http://<sandbox-host>:8100
SANDBOX_REPOSITORY_PATH=/repositories/validate
SANDBOX_HTTP_TIMEOUT_SECONDS=900
```

## Supported MVP checks

- Git clone and optional branch/commit checkout
- Node projects: installs dependencies with `pnpm`, `yarn`, `npm ci`, or `npm install` based on lockfiles; runs `npm run build` when present; smoke-starts `start`, `serve`, or `dev` scripts; then runs `npm run test` when present
- Chrome extension smoke: detects `manifest.json` with `manifest_version` and launches headless Chromium
- Python projects: installs `requirements.txt` with pip when present, then runs `compileall`
- PHP projects: runs `composer install` when `composer.json` exists, lints PHP files, smoke-starts `index.php` with PHP's built-in server, and verifies it with HTTP + headless Chromium
- Gradle/Maven projects only when wrapper files exist; JDK 17 is included in the sandbox image; Spring Boot projects are smoke-started with `bootRun`/`spring-boot:run` and verified with HTTP + headless Chromium

The sandbox image pre-installs common runtimes/build tools needed by these checks, including Node 22, Python, Chromium, PHP/Composer, JDK 17, git, and native build tools. It does **not** run arbitrary OS package installation from a repository by default; add system packages to the sandbox image deliberately so runs stay reproducible and safer.

Dependency downloads are cached in per-worker Docker volumes under `/cache` (`npm`, `pnpm`, `yarn`, `pip`, `composer`, `gradle`, and `maven`). This keeps every repository checkout fresh while avoiding repeated package downloads on Judge/Critic/Refiner retries or repeated validations. Caches are separated per sandbox worker to reduce cross-job lock contention. The gateway uses a consistent hash of the request body so identical repository-validation retries usually return to the same warm-cache worker.

## Cleanup and reuse policy

The fixed sandbox containers and dependency-cache volumes are reusable, but the cloned repository workdir is disposable. Each request creates a fresh workdir under `SANDBOX_WORK_ROOT` and removes it in a `finally` block after validation, regardless of success or failure. Workers also kill lingering server/browser/build processes tied to the workdir and remove stale workdirs on startup.

Judge/Critic/Refiner should consume the structured result and logs returned by the sandbox. If a repository fails Judge, retry by recloning the same repository/commit in a fresh workdir rather than reusing the failed workdir. A debug-only retention flag can be added later, but the safe default is always-delete.
