# Coldstart 0.1 to WarmStart 0.2

WarmStart is the product name. Coldstart remains only where compatibility would
otherwise break existing users or stored state.

## New use

Run `warmstart`. New managed sections use WarmStart markers.

## Existing use

- `coldstart` remains a command alias.
- `coldstart.*` Python imports remain valid.
- `.coldstart/` remains the state/config directory in 0.2.
- Existing Coldstart managed markers are recognized and retained in place.

No automatic state-directory rename occurs. A future state-format migration
needs its own versioned, tested migration rather than a cosmetic folder move.

## Historical language

Historical measurements and receipts keep the name they had when recorded.
“Cold start” also remains the ordinary name of the underlying problem.
