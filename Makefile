build:
	docker compose -f local.yml up --build -d --remove-orphans

up:
	docker compose -f local.yml up -d

down:
	docker compose -f local.yml down

down-v:
	docker compose -f local.yml down -v

banker-config:
	docker compose -f local.yml config

makemigrations:
	docker compose -f local.yml run --rm api python manage.py makemigrations

migrate:
	docker compose -f local.yml run --rm api python manage.py migrate

collectstatic:
	docker compose -f local.yml run --rm api python manage.py collectstatic --no-input --clear

superuser:
	docker compose -f local.yml run --rm api python manage.py createsuperuser

flush:
	docker compose -f local.yml run --rm api python manage.py flush

network-inspect:
	docker network inspect banker_local_nw

banker-db:
	docker compose -f local.yml exec postgres psql --username=alphaogilo --dbname=banker

test:
	docker compose -f docker/test.yml run --rm --build tests

test-postgres:
	docker compose -f docker/test-postgres.yml run --rm --build tests; \
	status=$$?; docker compose -f docker/test-postgres.yml down -v; exit $$status

lock-requirements:
	@docker compose -f docker/test.yml build tests >&2
	@{ echo "# Every installed package, transitive ones included, pinned to the versions the test suite ran with (CFG-02)."; \
	  echo "# Regenerate after changing base.txt or local.txt: make lock-requirements"; \
	  docker compose -f docker/test.yml run --rm -T tests python -m pip list --format=freeze 2>/dev/null \
	  | grep -v "^pip==\|^setuptools==\|^wheel==" | sort -f; } > requirements/constraints.txt.new \
	  && mv requirements/constraints.txt.new requirements/constraints.txt

check:
	docker compose -f docker/test.yml run --rm --build tests python manage.py check

STRIPE_EVENTS = checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired

# Local Stripe webhooks (test mode). The key is read from ../.env-stripe, outside the repository.
STRIPE_CLI = STRIPE_API_KEY="$$(sed -n 's/^STRIPE_SECRET_KEY=//p' ../.env-stripe)" stripe

stripe-webhook-secret:
	@secret="$$($(STRIPE_CLI) listen --print-secret)" || exit 1; \
	case "$$secret" in whsec_*) ;; *) echo "stripe listen did not return a signing secret" >&2; exit 1;; esac; \
	umask 077 && printf 'STRIPE_WEBHOOK_SECRET=%s\n' "$$secret" > ../.env-stripe-listen \
		&& echo "Wrote ../.env-stripe-listen; restart the api service to load it."

stripe-listen:
	@$(STRIPE_CLI) listen --events $(STRIPE_EVENTS) --forward-to localhost:8080/api/v1/accounting/stripe/webhook/
