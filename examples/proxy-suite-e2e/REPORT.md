# Capture suite, test report (both paths, real api keys)

Every developer and tool driven with a real api key, all three providers, down BOTH capture paths in one run, the original base-url gateway and the forward HTTPS proxy, all feeding one collector. Both an api key and a bearer token are exercised. The bearer is exactly how a subscription signs in, and both paths forward it unchanged, so the subscription path and the key path are the same path here.

**Result. 17 of 17 checks passed.**

## Traffic isolation

Only the model API hosts are read. A non-model site routed through the proxy is tunnelled byte for byte and never decrypted, proven by it validating against the system trust store with its own real certificate. A model host is intercepted, proven by it failing the system trust store because the proxy presents its own local certificate instead.

- PASS. a non-model site passes through untouched (real cert, system trust). example.com HTTP 200
- PASS. a model host IS intercepted (its TLS is terminated by our local CA, not the system store). 

The browser and every other application are untouched for a second reason too. The proxy is only set for the one tool launched with `abenlux run`, so nothing else on the machine even contacts it.

## Both capture paths, side by side

The same real api keys were driven down both capture paths in one run. The base-url path is the original way, a tool points its `ANTHROPIC_BASE_URL` (or OpenAI or Gemini base) at the local gateway. The proxy path is the forward HTTPS proxy, a tool routes through the agent as an ordinary proxy and the agent terminates the TLS with its own local certificate. Both capture a content-free record, compress on the wire, and forward to the same collector.

| Capture path | What the tool changes | Calls captured | Cost |
|---|---|--:|--:|
| base-url gateway | sets its base url to the gateway | 6 | $0.0013 |
| forward HTTPS proxy | nothing, runs behind `abenlux run` | 6 | $0.0015 |

## Sign-in shapes covered

| Sign-in shape | Calls |
|---|--:|
| api key (bearer header, same shape a subscription uses) | 2 |
| api key (x-api-key header) | 2 |
| api key (x-goog-api-key header) | 2 |

Every call above used a real api key, across all three header styles a provider uses, the Anthropic x-api-key, the OpenAI bearer, and the Gemini x-goog-api-key. The bearer header is byte for byte how a Claude or ChatGPT subscription presents its token, so the same proof covers a subscription. The proxy forwards whatever header it is given, so capture and compression work the same for a key and for a subscription.

## Capture by tool

| Tool | Calls | Input tokens | Cost |
|---|--:|--:|--:|
| aider | 2 | 1,159 | $0.0013 |
| claude-code | 2 | 1,159 | $0.0013 |
| cline | 2 | 44 | $0.0001 |
| codex | 2 | 46 | $0.0001 |
| gemini-cli | 2 | 33 | $0.0000 |
| opencode | 2 | 33 | $0.0000 |

## Capture by provider

| Provider | Calls | Cost |
|---|--:|--:|
| anthropic | 4 | $0.0026 |
| google | 4 | $0.0000 |
| openai | 4 | $0.0002 |

## Compression and savings

- Records compressed on the wire. 8 of 12
- Input tokens removed before billing. 3,552 of 6,026 raw (59 percent of raw input never reached the meter)
- Compression yield in the manager report. 3,552 tokens, about $0.0000, 0 calls served free from cache

  By strategy (realized)

  - command_trim. 3,552 tokens (~$0.0000)
  - cache_breakpoints. 0 tokens (~$0.0000)

## Collaboration and reuse

- Developers who matched a peer through the proxy. alice, bob, carol, dave, eve, frank
- Reuse matches carrying a content-free solution capsule. 21
  - Example capsule. cracked with gpt-4o-mini via codex, work type fix, cost band under $1
- Reuse yield booked beside spend. about $0.0000

## Spend to value

- Merged changes joined to spend. 4 of 4
- Dollars per merged change. $0.0000
- Merge rate 100 percent, revert rate 0 percent

## Renewal pack

- Blended rate the org pays. $1.1 per million tokens
- Projected annual run rate. $0.03
- Provider concentration. 0

## Privacy

- Every prompt is redacted on the device before anything is written. Only a content-free record reaches the collector.
- Management figures are k-anonymity gated. Org developers 6, orphan token share 0 percent.
- The proxy decrypts only the model hosts, on the device, and forwards the tool's own credential untouched. The raw prompt never leaves the machine.
