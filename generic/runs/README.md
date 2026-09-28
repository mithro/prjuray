# Run records

Orchestration scripts used for the fuzzing / DB build campaign on the
build host (absolute paths, kept as records of how each round was run).
Each heavy step goes through `generic/vrun.sh` (capped systemd scope in the
shared vivado.slice); dependent steps are chained in one script rather than
separate "wait until" loops, so a round is never launched twice.

- `memsample.sh`: samples memory.current/peak of every vivado.slice scope
  every 20 s into build/logs/mem_samples.log (for sizing --jobs).
- `chain_s7_17.sh`: r12 medium -> GTX-focused round (xc7k70t) -> Series7 DB.
- `us_rebuild3.sh`: UltraScale and UltraScale+ DB rebuilds in parallel.
- `chain_r13us.sh`: US/US+ r13 with a default-density and a --density 0.05 arm.
