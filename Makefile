CATALOG_IMG ?= quay-its.epfl.ch/svc0041/isas-fsd-catalog:latest

.PHONY: build
build: pullSecret/.dockerconfigjson
	docker build -t $(CATALOG_IMG) .

.PHONY: push
push:
	docker push $(CATALOG_IMG)

##############################################################################

.PHONY: dev
dev: venv opm

pullSecret/.dockerconfigjson:
	@echo >&2 "Please create suitable pullSecret/.dockerconfigjson for a dev build."
	@echo >&2 "This file should contain suitable Docker credentials to read images out of Quay."
	@echo >&2 "Obtain e.g. with"
	@echo >&2
	@echo >&2 "  oc extract --to=- secret/svc0041-svc0041-puller-pull-secret > pullSecret/.dockerconfigjson"
	@echo >&2
	exit 1

venv: requirements.txt requirements-dev.txt
	rm -rf $@; mkdir $@
	python3 -m venv venv
	@( . venv/bin/activate; set -e -x; \
	  pip3 install -r requirements.txt -r requirements-dev.txt )

.PHONY: test
test: dev
	env PATH="$$PATH:$$PWD/bin" ./venv/bin/python3 make-catalog.py \
	    $(wildcard *-olm.yaml) --configs-out configs --cache-out tmp/cache
	cat configs/index.yaml

OS := $(shell uname -s | tr '[:upper:]' '[:lower:]')
ARCH := $(shell uname -m | sed 's/x86_64/amd64/' | sed 's/aarch64/arm64/')

.PHONY: opm
OPM = ./bin/opm
opm: ## Download opm locally if necessary.
ifeq (,$(wildcard $(OPM)))
ifeq (,$(shell which opm 2>/dev/null))
	@{ \
	set -e ;\
	mkdir -p $(dir $(OPM)) ;\
	curl -sSLo $(OPM) https://github.com/operator-framework/operator-registry/releases/download/v1.23.0/$(OS)-$(ARCH)-opm ;\
	chmod +x $(OPM) ;\
	}
else
OPM = $(shell which opm)
endif
endif

clean:
	rm -rf config/ tmp/
