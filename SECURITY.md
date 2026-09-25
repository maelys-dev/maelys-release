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

## A signing key that may have been stolen

`share/allowed-signers` names the keys that may sign a release of this
fleet, and every product's release workflow verifies the tag's signature
against it. That file is read **at the socle commit each product pinned**,
which is what makes the rule predictable — and what makes a line written
here reach a product only when that product adopts a socle carrying it.

So the file rotates keys; it does not revoke them. In order, fastest first:

1. **Remove the key from the GitHub account** that holds it, and remove that
   account's ability to push tags to the products. GitHub then refuses to
   verify any new tag that key signs, and every release workflow of the
   fleet already requires GitHub's verdict before it looks at anything else
   — at every pin, old and new, the same evening. This is the only step that
   does not wait for an adoption.
2. **Retire the line here**, with `valid-before` at the moment the key was
   last trusted, and cut a socle release. Products receive it as they adopt.
   The tags the key signed while it was trusted keep verifying: the
   workflows judge at the moment GitHub saw the tag, not at the clock.
3. **Tell the products**, in their threads, which tags were signed by that
   key and when, so that each can decide what to re-verify. A published tag
   is never moved, so nothing is rewritten; what changes is what may be
   published next.

Never delete the line instead of retiring it: a deleted line makes the
releases that key signed unreplayable, and a failed publication is replayed
on the tag it already has.

## Who may sign a release

The maintainer of this repository decides which keys `share/allowed-signers`
names, and a key enters it through a pull request here — public, reviewable,
and recorded in the file's own history.

The request carries its own proof: **the commit that adds the line is signed
by the key it adds**. Nothing else establishes that whoever asks holds the
private half; a key registered on a GitHub account proves only that somebody
with that account uploaded a public key, which is also true of the
automation keys a release must not be signed with.

An operator who is not this repository's maintainer should count the steps
before relying on it: the pull request here, a socle release carrying it,
then the adoption that moves their product's pin. Until that adoption, their
product is judged by the list of the socle it pinned — and the maintainer of
this repository is, in practice, the fleet's signing authority. That is a
governance choice, written here so that it is one.

## Supported versions

The latest tag receives fixes. A product on an older pin upgrades by
re-adopting; published tags are never moved.
