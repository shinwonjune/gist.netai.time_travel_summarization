# Lake windowed playback benchmark

hz=5.0, chunk_seconds=60

| Scale | Rows | Chunks | Size(MB) | Ingest(s) | Cold seek mean/p95 (μs) | Warm seek (μs) | Stalls ON/OFF | Hit ON/OFF |
|-------|------|--------|----------|-----------|-------------------------|----------------|---------------|------------|
| 10obj×300s | 15000 | 5 | 1.30 | 0.11 | 3911 / 3964 | 2.196 | 0 / 4 | 80% / 0% |
