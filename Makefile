CATALOG_IMG ?= quay-its.epfl.ch/svc0041/isas-fsd-catalog:latest

.PHONY: build
build:
	docker build -t $(CATALOG_IMG) .

.PHONY: push
push:
	docker push $(CATALOG_IMG)

##############################################################################

.PHONY: dev
dev: venv

venv: requirements.txt requirements-dev.txt
	rm -rf $@; mkdir $@
	python3 -m venv venv
	@( . venv/bin/activate; set -e -x; \
	  pip3 install -r requirements.txt -r requirements-dev.txt )
