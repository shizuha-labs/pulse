# Makefile for Sphinx documentation
#

# You can set these variables from the command line, and also
# from the environment for the first two.
SPHINXOPTS    ?=
SPHINXBUILD   ?= sphinx-build
SPHINXAUTOBUILD ?= sphinx-autobuild
SOURCEDIR     = source
BUILDDIR      = _build

# Put it first so that "make" without argument is like "make help".
help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help Makefile clean livehtml

# Clean build directory
clean:
	rm -rf $(BUILDDIR)/*

# Live reload server for development
livehtml:
	$(SPHINXAUTOBUILD) "$(SOURCEDIR)" "$(BUILDDIR)/html" \
		--port 8080 \
		--host 0.0.0.0 \
		--watch ../tasks \
		--ignore "*.pyc" \
		--ignore "*~" \
		--ignore ".git/*" \
		--ignore "__pycache__/*" \
		$(SPHINXOPTS)

# Build and serve (one-time)
serve: html
	python -m http.server 8080 --directory $(BUILDDIR)/html

# Build only autodoc
apidoc:
	sphinx-apidoc -f -o $(SOURCEDIR)/api ../tasks --separate

# Catch-all target: route all unknown targets to Sphinx using the new
# "make mode" option.  $(O) is meant as a shortcut for $(SPHINXOPTS).
%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
