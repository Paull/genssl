.PHONY: dev dev-start dev-stop dev-reload dev-restart dev-status dev-logs start stop reload restart status logs build check migrate-v3

# Single-machine development commands. The controller owns only the PIDs it
# writes under .dev/, so it is safe to run beside unrelated local services.
dev: dev-start

dev-start:
	@bash scripts/devctl.sh start

dev-stop:
	@bash scripts/devctl.sh stop

dev-reload:
	@bash scripts/devctl.sh reload

dev-restart:
	@bash scripts/devctl.sh restart

dev-status:
	@bash scripts/devctl.sh status

dev-logs:
	@bash scripts/devctl.sh logs

# Short aliases for people who prefer `make start` over `make dev-start`.
start: dev-start
stop: dev-stop
reload: dev-reload
restart: dev-restart
status: dev-status
logs: dev-logs

build:
	@bun run build

check:
	@bun run check

migrate-v3:
	@python3 scripts/migrate_v3.py
