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

# Model diagrams (django-extensions graph_models + Graphviz) for docs/ARCHITECTURE.md.
DIAGRAMS = docker compose -f docker/test.yml run --rm -T --user "$$(id -u):$$(id -g)" \
	-e DJANGO_SETTINGS_MODULE=config.settings.diagrams tests python manage.py graph_models
DIAGRAM_OPTS = --arrow-shape normal --hide-edge-labels \
	--exclude-models TimeStampedModel,AppendOnlyRecord,AbstractUser,AbstractBaseUser,PermissionsMixin,Group,Permission

diagrams:
	@docker compose -f docker/test.yml build tests >&2
	@mkdir -p docs/diagrams
	$(DIAGRAMS) user_profile accounts cards user_auth $(DIAGRAM_OPTS) -o docs/diagrams/banking.png
	$(DIAGRAMS) accounting $(DIAGRAM_OPTS) -o docs/diagrams/ledger.png \
		-I ChartOfAccount,JournalEntry,JournalLine,PeriodClose,AuditEvent
	$(DIAGRAMS) accounting $(DIAGRAM_OPTS) -o docs/diagrams/receivables.png \
		-I Customer,Invoice,InvoiceLine,CreditNote,CustomerPayment,PaymentAllocation,AllocationRelease,PaymentRefund,StripeCheckoutSession,JournalEntry
	$(DIAGRAMS) accounting $(DIAGRAM_OPTS) -o docs/diagrams/payables.png \
		-I Supplier,Bill,BillLine,SupplierPayment,SupplierPaymentAllocation,JournalEntry,ChartOfAccount
	$(DIAGRAMS) accounting $(DIAGRAM_OPTS) -o docs/diagrams/bank_and_stripe.png \
		-I BankStatement,BankStatementLine,BankMatch,JournalLine,StripeBalanceTransaction,StripeWebhookEvent,JournalEntry,PaymentRefund,ChartOfAccount

check:
	docker compose -f docker/test.yml run --rm --build tests python manage.py check

STRIPE_EVENTS = checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired

# Local Stripe webhooks (test mode). The key is read from ../.env-stripe, outside the repository:
# STRIPE_CLI_KEY (a restricted key with only Debugging Tools: Write) if set, else STRIPE_SECRET_KEY.
# The app's restricted key cannot start a stripe listen session.
STRIPE_CLI_API_KEY = $$(k=$$(sed -n 's/^STRIPE_CLI_KEY=//p' ../.env-stripe); [ -n "$$k" ] || k=$$(sed -n 's/^STRIPE_SECRET_KEY=//p' ../.env-stripe); echo "$$k" | tr -d '"')
STRIPE_CLI = STRIPE_API_KEY="$(STRIPE_CLI_API_KEY)" stripe

stripe-webhook-secret:
	@secret="$$($(STRIPE_CLI) listen --print-secret)" || exit 1; \
	case "$$secret" in whsec_*) ;; *) echo "stripe listen did not return a signing secret" >&2; exit 1;; esac; \
	umask 077 && printf 'STRIPE_WEBHOOK_SECRET=%s\n' "$$secret" > ../.env-stripe-listen \
		&& echo "Wrote ../.env-stripe-listen; restart the api service to load it."

stripe-listen:
	@$(STRIPE_CLI) listen --events $(STRIPE_EVENTS) --forward-to localhost:8080/api/v1/accounting/stripe/webhook/

# Demo production stack on this server, behind the shared edge Caddy (docs/DEPLOYMENT.md).
PROD = docker compose -f production.yml

# nginx resolves api once at start; restart it so it follows a recreated api container.
prod-up:
	$(PROD) up -d --build --remove-orphans
	$(PROD) restart nginx

prod-down:
	$(PROD) down

prod-logs:
	$(PROD) logs -f --tail 100

prod-ps:
	$(PROD) ps

prod-manage:
	$(PROD) exec api python manage.py $(cmd)
