# Build and package the SprintFlow Rich Artifacts plugin.
#
#   make dist      reproducible install from the lockfile, production build,
#                  and a tar.gz laid out the way Mattermost expects
#   make deploy    upload that archive to the running Mattermost and enable it
#   make clean     remove build output (node_modules is left alone)

PLUGIN_ID := com.sprintflow.mermaid
VERSION   := $(shell node -p "require('./plugin.json').version")
BUNDLE    := dist/$(PLUGIN_ID)-$(VERSION).tar.gz

.PHONY: all deps build server dist deploy clean check-types

all: dist

## deps: install exactly what the lockfile pins
deps:
	cd webapp && npm ci --no-audit --no-fund

## check-types: tsc with no emit
check-types:
	cd webapp && npm run check-types

## build: production webapp bundle (single chunk — Mattermost serves one file)
build:
	cd webapp && npm run build

## server: compile the Go component (in a container; no host Go needed).
## It embeds React, Babel and the webapp's lazy chunks, so it depends on the
## webapp build and on `make deps` having run.
server: build server/assets/react.js server/assets/react-dom.js server/assets/babel.js chunks
	docker run --rm -v "$(CURDIR)/server:/src" -w /src \
		-e GOFLAGS=-mod=mod -e GOCACHE=/tmp/gocache -e GOMODCACHE=/tmp/gomod \
		golang:1.23 sh -c 'GOOS=linux GOARCH=amd64 go build -trimpath -o dist/plugin-linux-amd64 .'
	docker run --rm -v "$(CURDIR)/server:/src" -w /src alpine:3 chown -R $(shell id -u):$(shell id -g) /src

server/assets/react.js: webapp/node_modules/react/umd/react.production.min.js
	cp $< $@

server/assets/react-dom.js: webapp/node_modules/react-dom/umd/react-dom.production.min.js
	cp $< $@

server/assets/babel.js: webapp/node_modules/@babel/standalone/babel.min.js
	cp $< $@

## chunks: copy the lazily loaded webapp chunks into the Go component's embed dir
.PHONY: chunks
chunks:
	rm -rf server/assets/chunks && mkdir -p server/assets/chunks
	cp webapp/dist/chunk.*.js server/assets/chunks/

## dist: the installable archive
dist: server
	rm -rf dist/$(PLUGIN_ID) $(BUNDLE)
	mkdir -p dist/$(PLUGIN_ID)/webapp/dist dist/$(PLUGIN_ID)/server/dist
	cp plugin.json dist/$(PLUGIN_ID)/plugin.json
	cp webapp/dist/main.js dist/$(PLUGIN_ID)/webapp/dist/main.js
	cp server/dist/plugin-linux-amd64 dist/$(PLUGIN_ID)/server/dist/plugin-linux-amd64
	tar -czf $(BUNDLE) -C dist $(PLUGIN_ID)
	rm -rf dist/$(PLUGIN_ID)
	@echo "built $(BUNDLE) ($$(du -h $(BUNDLE) | cut -f1))"

## deploy: install into the running Mattermost (needs admin credentials in ../../.env)
deploy: dist
	python3 ../../scripts/install_plugin.py $(BUNDLE)

clean:
	rm -rf dist webapp/dist server/dist server/assets/react.js server/assets/react-dom.js server/assets/babel.js server/assets/chunks
