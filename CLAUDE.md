# kube-capstone

A Kubernetes deployment capstone project: a small "Hotel Booking" app used as
the vehicle to practice and demonstrate real Kubernetes/DevOps skills
(Deployments, StatefulSet, CronJob, NetworkPolicy, Kustomize, Helm, CI/CD,
ArgoCD) for job applications.

See `docs/k8s-progress.md` for the detailed running log of what's been built,
verified, and discovered along the way.

## App architecture

Hotel Booking: search hotels/rooms, book a room, async payment confirmation,
auto-cancel stale holds.

- `frontend/` — nginx + vanilla JS. Auth forms, hotel search, booking list.
  Backend location is templated via nginx's built-in envsubst
  (`templates/default.conf.template`, `API_HOST`/`API_PORT` env vars) —
  never baked into the image.
- `api/` — FastAPI. JWT auth (register/login), hotel/room search (cached in
  Redis), booking create/list/cancel. Booking creation inserts a `pending`
  row and enqueues a job on Redis; double-booking is prevented at the
  database layer by a Postgres `EXCLUDE USING gist` constraint on
  `(room_id, daterange(check_in, check_out))`, not application logic.
- `worker/` — consumes the Redis queue, simulates payment processing
  (`PROCESSING_DELAY_SECONDS`, `PAYMENT_FAILURE_RATE`), flips booking status
  to `confirmed`/`failed`. Also ships `cancel_stale.py`, a one-shot script
  (same image, different command) that cancels stale `pending` bookings —
  runs as a Kubernetes CronJob.
- `redis` — cache (room availability search) + job queue (booking
  confirmation), two distinct uses of the same instance.
- `postgres` — StatefulSet + PVC. Schema: `users`, `hotels`, `rooms`,
  `bookings`.

All service config comes from environment variables (`DB_HOST`, `DB_PORT`,
`REDIS_HOST`, `JWT_SECRET`, etc.) — nothing hardcoded — so the same image
works unchanged across dev/staging/prod.

## Local development

```bash
docker compose up --build
# localhost:8080 — frontend
# localhost:8000 — api (health: /healthz, /readyz, /metrics)
# localhost:8001 — worker (health: /healthz, /readyz, /metrics)
docker compose run --rm cancel-stale   # test the CronJob script manually
```

## Kubernetes

Manifests live in `k8s/base/` (plain Kustomize, no overlays yet).

```bash
kubectl apply -k k8s/base -n hotel-dev
```

**Important cluster-specific quirk:** the `docker-desktop` kubectl context
here is actually a 3-node **kind** cluster (`desktop-control-plane`,
`desktop-worker`, `desktop-worker2`), not single-node Docker Desktop
Kubernetes. Locally built images are **not** automatically visible to it —
each kind node has its own containerd image store, separate from the
`docker build` image cache. This mattered a lot before CI/CD existed (see
`docs/k8s-progress.md` for the `docker save | ctr images import` workaround
used at the time) — now that images are pulled from Docker Hub (below),
that workaround is no longer needed for anything CI builds and pushes.

CI/CD is wired up (`.github/workflows/build-push.yml`): every push to `main`
touching `api/`, `worker/`, or `frontend/` builds and pushes all 3 images to
Docker Hub tagged by commit SHA, then a second job bumps the tag in
`k8s/base/kustomization.yaml`'s `images:` transformer and commits that back
to `main` automatically (as `github-actions[bot]`). `imagePullPolicy` is
`IfNotPresent` everywhere now, referencing `jun9187/hotel-booking-*` — no
local image loading needed for anything built through this pipeline. `git
pull` then `kubectl apply -k` picks up whatever the bot last committed.

## Registries

- **GitHub**: `jun9187/kube-capstone` (public), under a personal GitHub
  account — deliberately separate from the work account/email used
  elsewhere on this machine. This repo's git identity is set **locally**
  (`git config user.email`, not `--global`) to
  `150773849+jun9187@users.noreply.github.com` so commits attribute
  correctly without touching global config.
  - `gh auth` has both accounts; `jun9187` should be active for this repo.
    Its token has the `workflow` scope (added via `gh auth refresh -h
    github.com -s workflow`), needed for pushing `.github/workflows/*.yml`.
- **Docker Hub**: images pushed as `jun9187/hotel-booking-api`,
  `jun9187/hotel-booking-worker`, `jun9187/hotel-booking-frontend`, each
  public, tagged by git commit SHA (not just `:latest` — Kubernetes/ArgoCD
  only trigger a rollout when the image tag string actually changes).
  Credentials for CI are stored as GitHub Actions repo secrets
  (`DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`) — never in any committed file.

## Conventions / things to not re-litigate

- Image names describe the app (`hotel-booking-*`), not the project folder
  (`kube-capstone-*` was the original Docker Compose default and was
  deliberately renamed away from).
- Secrets: `k8s/base/secrets.env` (gitignored, real dev values) +
  `k8s/base/secrets.env.example` (committed template), consumed via
  Kustomize's `secretGenerator`. Never put real secret values in a
  committed file.
- **Never run `git commit` or `git push` without an explicit ask each
  time** — finishing a task does not imply consent to persist it to the
  remote.
