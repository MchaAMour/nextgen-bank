# NextGen Bank

A Django REST banking backend (from the API-Imperfect course), extended with a
double-entry **accounting platform**: a ledger, invoicing and payments, supplier
bills, Stripe Checkout, bank reconciliation and financial reports.


## Documentation

| Read | For |
| --- | --- |
| [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) | What the course built and what this fork adds |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Runtime, apps, data model diagrams, main flows |
| [docs/LEDGER.md](docs/LEDGER.md) | How the ledger works and how its integrity is enforced |
| [docs/DEMO.md](docs/DEMO.md) | Step-by-step walkthrough of the live demo |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running the demo stack |

## Development

```sh
make build && make up     # local stack (ports bound to localhost)
make test                 # test suite on SQLite
make test-postgres        # test suite on PostgreSQL 16
make diagrams             # regenerate docs/diagrams
```
