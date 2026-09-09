# Security policy

## Reporting a vulnerability

Report a suspected vulnerability privately, through the GitHub advisory form
of this repository (`Security` tab, `Report a vulnerability`). Do not open a
public issue for it, and do not describe it in a pull request.

## Scope

This repository writes workflows and files into other repositories, so its
supply chain is its main surface: the reusable workflows of
`.github/workflows/`, the files `adopt` generates, the pinned actions, and
the vendored `bin/maelys_cli.py` with its recorded digest. A weakness that
lets a product receive content this repository did not generate belongs
here.

A vulnerability in a released product belongs to that product's repository,
and one in the agent-cli/v2 contract to maelys-dev/agent-cli-spec.

## Supported versions

The latest tag receives fixes. A product on an older pin upgrades by
re-adopting; published tags are never moved.
