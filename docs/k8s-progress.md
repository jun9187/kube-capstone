# Kubernetes deployment progress log

Running log of the capstone project — what's been built, verified, and
learned along the way. See `CLAUDE.md` at the repo root for the current
architecture/conventions snapshot; this file is the chronological history.

## 2026-09-09 — App built, registries set up, base manifests to be hand-written

### App: Hotel Booking

Landed on a hotel booking domain (after considering a bookmark manager and
an appointment scheduler) specifically because it forces a real concurrency
problem — preventing double-booking a room — solved with a Postgres
`EXCLUDE USING gist` constraint rather than application-level locking. Redis
gets two genuinely different jobs (search cache + confirmation queue), and
the CronJob has real business purpose (auto-cancel stale holds) instead of
generic cleanup.

Verified locally via Docker Compose:
- Register/login (JWT), search hotels/rooms, book a room
- Overlapping booking for the same room → `409` (exclusion constraint
  working, confirmed by directly attempting an overlap)
- Async worker confirmation: `pending` → `confirmed`/`failed`
- Cancelling a booking frees the room (confirmed by successfully rebooking
  the exact same dates afterward)
- Full stack survives `docker compose down && up` with data intact
- `cancel_stale.py` runs standalone via `docker compose run --rm cancel-stale`

### Kubernetes base manifests (`k8s/base/`) — first pass, since removed

AI-authored a first pass of `k8s/base/`: ConfigMap, Kustomize
`secretGenerator` (reads gitignored `secrets.env`), Postgres StatefulSet +
headless Service + PVC, Redis/api/worker/frontend Deployments + Services,
`cancel-stale-bookings` CronJob, NetworkPolicy (default-deny-ingress +
explicit allow rules per hop, including a monitoring-namespace scrape
allowance for later). Applied to the cluster and verified (see below).

**These manifest files were deleted afterward** so they could be hand-written
from scratch instead, as a learning exercise — the goal is to actually
practice writing Deployment/StatefulSet/CronJob/NetworkPolicy YAML rather
than have it generated. The discoveries and verification results below are
still valid (they're facts about the cluster/environment, not about the
specific YAML), but the `k8s/base/` manifests themselves need to be rewritten
and re-verified once that's done. The still-running `hotel-dev` namespace in
the kind cluster was deployed from the now-deleted manifests and was left
alone — it'll get overwritten once the hand-written version is applied.

**Discovery: the `docker-desktop` kubectl context is actually a 3-node kind
cluster** (`desktop-control-plane` + 2 workers), not single-node Docker
Desktop Kubernetes as assumed. Symptom: `ErrImageNeverPull` on pods scheduled
to the second worker node. Root cause: each kind node has its own
containerd image store, isolated from the `docker build` image cache —
locally built images aren't automatically visible cluster-wide. Fixed the
same way `kind load docker-image` works internally:

```bash
docker save <image>:<tag> | docker exec -i <node> ctr -n k8s.io images import -
```

run against both `desktop-worker` and `desktop-worker2` for every image.

**Verified NetworkPolicy is actually enforced**, not just applied — this
isn't guaranteed on kind's default CNI (kindnet), so it was tested directly
rather than assumed: `kubectl exec`'d into a frontend pod and tried `nc` to
postgres/redis/worker (all timed out — correctly denied) and to api (worked
instantly — correctly allowed).

Verified full app flow against the real cluster via
`kubectl port-forward svc/frontend` — same checks as the Compose version,
all passing.

### Registries

- **GitHub**: created `jun9187/kube-capstone` (public) under a personal
  account, distinct from the work account/email (`benjaminYTLAIC` /
  `benjamin.chew@ytlaicloud.com`) already configured globally on this
  machine. Added the personal account via `gh auth login --web` (device
  flow), and set this repo's git identity **locally**
  (`git config user.email`, scoped to the repo, not `--global`) so commits
  attribute correctly without touching the global work config.
- **Docker Hub**: pushed all 3 images as separate repos (one repo per
  image is the actual registry data model / industry norm, not a stylistic
  choice — confirmed public via the Docker Hub API,
  `is_private: False` on all three), each tagged by git commit SHA *and*
  `:latest`. Commit-SHA tagging matters mechanically, not just for
  hygiene: Kubernetes/ArgoCD only trigger a rollout when the image tag
  string in the manifest actually changes — a static `:latest` tag means
  a new push produces zero diff in the Deployment spec, so nothing rolls
  out without a manual `kubectl rollout restart`.

### Image naming fix

Original images were auto-named `kube-capstone-*` by Docker Compose (project
folder name + service name) — renamed to `hotel-booking-*` (what the app
actually is) across `docker-compose.yaml` (explicit `image:` fields added),
all `k8s/base/*.yaml` image references, and both kind nodes' containerd
stores. Confirmed it was a real rollout (new ReplicaSets created, old ones
scaled to 0), not just a manifest edit.

### Explored, not adopted: `kompose convert`

Tried converting `docker-compose.yaml` with `kompose` as an experiment (the
snap-installed v1.21.0 couldn't parse the modern Compose Specification
format — had to download v1.38.0 directly from GitHub releases to scratch).
Output was a useful reference but not something to switch to — gaps versus
the hand-written `k8s/base/`:

- Postgres comes out as a **Deployment + PVC**, not a StatefulSet (breaks on
  rescheduling/scaling — no stable identity)
- Secrets (`JWT_SECRET`, `DB_PASSWORD`) inlined as **plaintext** literal env
  values directly in the Deployment YAML
- Only a liveness probe (copied from the compose healthcheck), no readiness
  probe
- No resource requests/limits, no NetworkPolicy, single replica everywhere

### Decision: database strategy per environment

- **dev**: self-hosted Postgres in-cluster (StatefulSet + PVC), for offline/
  no-cost local iteration.
- **staging** and **prod**: both external **Supabase** (managed Postgres),
  mirroring each other exactly — deliberately chosen over "only prod
  external" so staging is a true rehearsal of prod (dev/prod parity
  principle — staging's job is to catch the class of bugs that only show up
  against the real backing service: SSL handshake, connection limits,
  managed-service latency). Only dev gets to diverge, since it's a
  throwaway individual sandbox, not a rehearsal environment.
- Mechanism: Postgres StatefulSet lives in a Kustomize **Component**
  (`k8s/components/postgres/`, `kind: Component`), included via
  `components:` only in `overlays/dev/kustomization.yaml`. `staging`/`prod`
  overlays omit it entirely and instead patch the ConfigMap/Secret
  (`DB_HOST`, credentials) to point at their respective Supabase project.
  Redis stays in `base/` (genuinely common to all three envs — Supabase
  doesn't offer a queue/cache, so this piece doesn't diverge).
- Known free-tier caveat (both staging and prod Supabase projects will be
  free tier, since this is a learning project, not a real business):
  free-tier projects **auto-pause after ~1 week of no API activity** and
  need a manual resume — the one behavior that doesn't match real prod.
  Worth remembering before demoing this live (e.g. in an interview) after a
  quiet period — the first request may hang while the project wakes up.
  Also no automated backups on free tier — an honest gap to acknowledge
  rather than paper over if asked. Need to check Supabase's current
  per-organization free-project limit before assuming both staging and prod
  can each get their own free project.
- Also need `sslmode=require` (or equivalent) added to the api/worker
  Postgres connection when pointed at Supabase — not needed against the
  in-cluster Postgres today, easy to forget until it fails.

### Decision: config/secrets folder layout, and the GitOps secrets problem

`k8s/base/config/` holds the ConfigMap and the `secretGenerator` setup:
- `configmap.yaml` — hand-written, plain non-sensitive values (`DB_HOST`,
  `DB_PORT`, `REDIS_HOST`, `JWT_EXPIRE_MINUTES`, etc.)
- `secrets.env` (gitignored) / `secrets.env.example` (committed) — only the
  genuinely sensitive values (`DB_PASSWORD`, `JWT_SECRET`)
- `kustomization.yaml` — declares `secretGenerator: [{name: hotel-secrets,
  envs: [secrets.env]}]`

Deliberately did **not** put non-sensitive config into the Secret "for
simplicity" — the split isn't stylistic, it's what lets RBAC and git-commit
policy actually differentiate the two (Secrets need tighter access control
and can never be committed; ConfigMap values are fine to read broadly and
fine to diff in git history).

Each overlay whose secret values differ from base gets its **own**
`secrets.env` (gitignored) + `secrets.env.example` (committed), merged over
base's via `secretGenerator: [{name: hotel-secrets, behavior: merge, envs:
[secrets.env]}]` in that overlay's `kustomization.yaml`. Concretely:
`overlays/staging/secrets.env` (staging's own Supabase project credentials)
and `overlays/prod/secrets.env` (prod's own, separate Supabase project
credentials) — `dev` has no override, since base's values already are the
dev defaults.

**Open problem, not yet solved: where do the real values in
`overlays/staging/secrets.env` and `overlays/prod/secrets.env` actually come
from once ArgoCD is deployed?** Today (manual `kubectl apply -k` run from a
laptop) the answer is "typed in by hand, sourced from each Supabase
project's dashboard" — that file exists locally and is never committed.
That stops working the moment ArgoCD is doing pull-based deploys straight
from Git, since it has no laptop-local file to read and, by design, the
gitignored file was never committed for it to see either.

Standard real-world fixes for GitOps + secrets (well-known hard problem,
not a gap unique to this project):
1. **Sealed Secrets** (Bitnami) — encrypt the value with `kubeseal` into a
   `SealedSecret` resource that *is* safe to commit (only the in-cluster
   controller's private key can decrypt it). No external account needed —
   **this is the one planned for this project**, added once ArgoCD is
   actually being set up.
2. **External Secrets Operator** — manifest holds only a *reference* to an
   external secrets manager (Vault, AWS/GCP Secrets Manager, Doppler); an
   in-cluster operator syncs the real value in. Closer to how larger
   companies do it, but needs a real secrets-manager account behind it.
3. **Manual bootstrap outside GitOps** — `kubectl create secret` once,
   directly against the live cluster, excluded from what ArgoCD manages.
   Simplest, but breaks full reproducibility if the cluster is ever rebuilt.

Decided to apply Sealed Secrets **uniformly across dev, staging, and prod**
once that step arrives — including dev, even though its values are harmless
placeholders, not real credentials. Deliberately not special-casing dev as
"just commit it, it's not really sensitive" — modeling consistent secret
handling with no exceptions matters more here than the minor convenience,
given the point of this project is to demonstrate disciplined practice.

### Next steps

- [ ] Hand-write `k8s/base/` manifests from scratch (learning exercise —
      replaces the deleted AI-authored first pass): ConfigMap, Secret via
      `secretGenerator`, Postgres StatefulSet + headless Service + PVC,
      Redis/api/worker/frontend Deployments + Services, the
      `cancel-stale-bookings` CronJob, NetworkPolicy. Re-verify against the
      cluster once written (re-run the NetworkPolicy enforcement check, the
      full app flow via port-forward, etc.)
- [ ] Kustomize overlays: `dev`/`staging`/`prod` — ClusterIP in base,
      NodePort (dev) vs Ingress+TLS (staging/prod), replica/resource sizing
      per env, `postgres` Component included only in `dev` (see database
      strategy decision above), staging/prod ConfigMap+Secret patches
      pointing at their Supabase project instead
- [x] GitHub Actions CI: build + push to Docker Hub tagged by commit SHA,
      write the new tag back into the k8s manifests (see 2026-09-10 entry)
- [x] ArgoCD installed; `Application` manifests for dev/staging written
      (see 2026-09-10 entry) — not yet synced live
- [ ] Sealed Secrets / Reloader: on hold, see 2026-09-10 decision below —
      current secrets approach is a manual stopgap, not real GitOps
- [ ] `prod/` overlay — same shape as `staging/`, separate Supabase project
- [ ] Helm: `kube-prometheus-stack` for monitoring, as a plain ArgoCD Helm
      source (deliberately *not* inflating it through Kustomize —
      considered `kustomize.buildOptions: --enable-helm` and decided the
      added repo-server complexity wasn't worth it for this project)
- [ ] Ingress + cert-manager for staging/prod (replacing NodePort), also a
      better demo than raw NodePorts — blocked on this specific kind
      cluster only having port 6443 mapped to the host (see 2026-09-10
      entry); would need cluster recreation to fix properly, which isn't
      worth the disruption right now

## 2026-09-10 — CI/CD, ArgoCD, and the secrets-in-GitOps stopgap

### CI/CD pipeline

`.github/workflows/build-push.yml`, two jobs:
- `build-and-push` — matrix builds api/worker/frontend, pushes each to
  Docker Hub tagged by commit SHA and `:latest`
- `update-manifests` (needs the above) — installs `kustomize`, runs
  `kustomize edit set image` to bump all 3 tags in
  `k8s/base/kustomization.yaml`'s `images:` transformer, commits the
  change back to `main` as `github-actions[bot]`

Switched `api`/`worker`/`frontend` from local-only images
(`imagePullPolicy: Never`, loaded via the `ctr images import` workaround)
to Docker Hub (`jun9187/hotel-booking-*`, `imagePullPolicy: IfNotPresent`).
Verified live: applied to `hotel-dev`, new pods pulled from Docker Hub over
the real network and came up healthy — the local image-loading workaround
is no longer needed for anything built through this pipeline.

Prerequisite fixed: `jun9187` gh token was missing the `workflow` scope,
needed before `git push` accepts `.github/workflows/*.yml` — fixed via
`gh auth refresh -h github.com -s workflow`.

### ArgoCD

Installed via the official stable manifests into an `argocd` namespace.
One real install snag: `kubectl apply` failed on the `applicationsets`
CRD (`metadata.annotations: Too long`) — a known issue with very large
CRDs and client-side apply's last-applied-config annotation. Fixed with
`kubectl apply --server-side --force-conflicts` instead.

Wrote `argocd/applications/dev.yaml` and `argocd/applications/staging.yaml`
— each points at this repo's `k8s/overlays/{dev,staging}`, destination
namespace `hotel-{dev,staging}`, `automated: {prune: true, selfHeal: true}`.
Not yet applied/synced — see the secrets blocker below.

### Decision: secrets-in-GitOps stopgap (Sealed Secrets put on hold)

Confirmed the anticipated problem is real: `k8s/overlays/{dev,staging}`
and `k8s/base/{config,api}` all had `secretGenerator`s reading gitignored
`secrets.env` files. ArgoCD's repo-server checks out a clean clone from
GitHub — those files were never pushed, so `kustomize build` would fail
before ever reaching the cluster.

Started implementing Sealed Secrets (installed the controller + `kubeseal`
CLI, both still installed and running) but paused partway through — the
`secretGenerator` → `SealedSecret` conversion turned out to give up the
content-hash-suffix mechanism (Kustomize hashes plaintext at build time;
a `SealedSecret`'s whole point is the plaintext is never present in
anything git-tracked, so those two properties are mutually exclusive).
The real fix for that gap is a separate controller — Stakater's Reloader,
which watches the *decrypted* Secret object directly and patches a
rollout-triggering annotation itself, independent of Kustomize. Not
implemented yet — parked for later.

**Interim decision: manual stopgap, chosen deliberately over finishing
Sealed Secrets right now.** Removed the `secretGenerator` blocks from
`base/config`, `base/api`, and `overlays/staging` entirely (nothing left
in git referencing the gitignored files), and created the real
`db-credentials`/`api-secrets` Secrets directly in-cluster by hand
(`kubectl create secret`, values read from the existing local
`secrets.env` files, never typed inline) in both `hotel-dev` and
`hotel-staging`. Since these Secrets are now never part of what Kustomize
renders, ArgoCD's prune/sync logic simply never sees or touches them —
no special "ignore this resource" config needed on ArgoCD's side, that
falls out automatically from removing them from git.

Known limitation, accepted deliberately: **this is not real GitOps for
secrets** — if either namespace/cluster were ever rebuilt from scratch,
these two Secrets would need recreating by hand again, with no record in
git of that step. Revisit with Sealed Secrets + Reloader once that's
worth prioritizing again.

**Bonus: this stopgap fixed a real, independently-discovered bug.**
`overlays/staging/kustomization.yaml`'s old `secretGenerator` only had a
`behavior: merge` entry for `db-credentials` — never one for `api-secrets`.
Result: staging's `api` Deployment was silently signing/verifying JWTs
with **dev's** `JWT_SECRET`, not the fresh one generated specifically for
staging (which sat unused as an orphaned key inside `db-credentials`
instead). Confirmed by decoding both values against the live rendered
manifest. Manually creating staging's `api-secrets` Secret directly fixed
this correctly — verified afterward that the live Secret's `JWT_SECRET`
matches staging's own generated value, not dev's.

### Discovery: this kind cluster can't do real Ingress either

Before deciding to defer Ingress+TLS: checked whether this kind cluster
was created with the `extraPortMappings` (host 80/443 → node) that
`ingress-nginx`-on-kind normally needs. It wasn't — `docker port
desktop-control-plane` shows only `6443` (the API server) mapped to the
host. That mapping is baked in at cluster-creation time and can't be
added retroactively. Also couldn't find the original kind config
anywhere on disk, and this cluster's kubectl context (`docker-desktop`)
doesn't match the name a plain `kind create cluster --name desktop` would
produce (`kind-desktop`) — something set this cluster up in a way whose
exact provenance isn't fully known. Decided against recreating the
cluster to fix this, given the disruption (every namespace, every loaded
image, would need redoing) versus the uncertainty of cleanly reproducing
the current setup. Ingress, when built, will be validated via
`port-forward` instead of real browser-from-Windows reachability — same
honest limitation already accepted for NodePort earlier.
