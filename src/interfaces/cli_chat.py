"""Terminal chat transport. A thin shell over the router.

STUB -- Step 4. The docstring below is the specification.

Owned by: the interfaces layer. Called by the user. Calls: `router.route`.

A transport, not an implementation (CLAUDE.md section 2): it turns typed lines
into `route()` calls and prints what comes back. The voice interface in Step 5 is
a sibling of this file and shares everything below `route`.

Intended behaviour:

  - start unscoped; `use tenant <name>` binds a TenantContext via the resolver
  - on an ambiguous tenant name, print the ranked candidates and ask
  - print the executed SQL alongside every data answer -- the demo depends on
    being able to show the guard's rewritten query with its injected predicate
  - `scope` prints the current session scope, so a viewer can see that a refusal
    of a cross-tenant question is about authority and not about capability
"""

from __future__ import annotations


def main() -> None:
    """Run the terminal chat loop."""
    raise NotImplementedError("Step 4: CLI chat interface")


if __name__ == "__main__":
    main()
