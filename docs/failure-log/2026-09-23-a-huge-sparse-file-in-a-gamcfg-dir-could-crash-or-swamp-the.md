# 2026-09-23 — A huge sparse file in a gamcfg-* dir could crash (or swamp) the startup sweep

- **Symptom:** found in review cleanup: `_shred_dir`'s `_zero_file` built its zeros as
  `b"\x00" * st_size`. A 256 MiB sparse file there cost 256 MiB of memory in the test; a TiB one
  planted by a same-user process raises `MemoryError` where the allocation is refused, which the
  sweep (`except OSError`) doesn't catch, so startup crashes every launch. On macOS `malloc` grants
  even a TiB, so the sweep would memset it instead.
- **Cause:** the overwrite sized its buffer from the file, unbounded; `setup.py`'s `_wipe_file`
  already chunked, this one didn't.
- **Why not caught:** every wipe test used files of a few bytes; nothing planted a large one.
- **Fix:** zero in 64 KiB chunks, at most 1 MiB per file (ours are a few KB). Chunks alone would
  fill the disk for a sparse TiB, hence the cap.
- **Prevention:** `test_ephemeral.py::test_zeroing_a_huge_sparse_file_is_bounded_in_memory_and_disk`
  (peak memory < 1 MiB, head zeroed, disk not filled; fails on the old code),
  `test_sweep_removes_a_dir_holding_a_huge_sparse_file`, `test_zeroing_covers_a_file_across_several_chunks`.
  The TiB case itself is not run: on the old code it would swamp the test machine.
