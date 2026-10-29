# Demo deployment

The demo runs on the same server as the local stack (`srv1915831`), at
`https://next-gen-bank.srv1915831.hstgr.cloud`. It is a demo, set up as close to
production as practical: gunicorn, `DEBUG` off, HTTPS only, its own database,
broker and Stripe sandbox.

## Layout

| Part | Where |
| --- | --- |
| Compose project | `production.yml`, project `next-gen-bank` |
| Settings and secrets | `/srv/next-gen-bank/.env.production` (root only, mode 600, never committed) |
| TLS and public ports | The shared edge Caddy (`radian-edge-caddy-1`), site file `/srv/radian/caddy/sites/next-gen-bank.caddy` |
| Edge network | nginx joins the external `radian-edge` network as `next-gen-bank-web`; everything else stays on the project's internal network |
| Email | Mailpit inside the stack catches all mail; set `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and `EMAIL_USE_TLS` for a real SMTP provider |
| Stripe | Sandbox `banking-demo` (`acct_1UVselELdbrRRAW5`), restricted key; the local stack uses `banking-test`. Each Stripe account must belong to one ledger only, or neither reconciles. |

Only Caddy publishes public ports on this server; Docker-published ports bypass
ufw. Flower and Mailpit bind to localhost: tunnel with
`ssh -N -L 5556:localhost:5556 -L 8026:localhost:8026 root@185.166.39.74` and open
`localhost:5556` (Flower, credentials in the env file) and `localhost:8026`.

## Commands

| Command | Does |
| --- | --- |
| `make prod-up` | Build and start (or update) the stack; the api runs migrations and collectstatic on start |
| `make prod-ps`, `make prod-logs` | Status and logs |
| `make prod-manage cmd="createsuperuser"` | Any management command |
| `make prod-down` | Stop the stack; volumes (database, broker) are kept |

After a first start, run `make prod-manage cmd=setup_accounting_roles` and
`make prod-manage cmd=schedule_stripe_sync`.

## Settings

`/srv/next-gen-bank/.env.production` holds `DJANGO_SETTINGS_MODULE`
(`config.settings.production`), `DEBUG="false"`, `SECRET_KEY`, `SIGNING_KEY`,
`DOMAIN`, the Postgres, RabbitMQ (`RABBITMQ_DEFAULT_USER/PASS`, applied only when
the broker volume is first created) and Celery URLs, Flower credentials, email,
and the Stripe settings (`STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`,
`STRIPE_ACCOUNT_ID`, `STRIPE_WEBHOOK_SECRET` and the checkout return URLs on the
public host name). Boolean settings accept only `true`, `1` or `yes`.

The repository's earlier `do_server_deploy.sh` (SSH to a DigitalOcean server and
`docker system prune -af`) was removed: the prune would delete other projects'
images on this shared server.
