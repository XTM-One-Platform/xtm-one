# Indicator decay re-check

OpenCTI's indicator decay lowers an indicator's score over time and revokes the indicator when the score reaches the decay rule's revoke score. Some indicators are revoked while their observable is still malicious. XTM One ships two built-in agents that re-check every indicator about to be revoked:

- **Indicator Decay Guardian** finds the indicators that decay revokes in the next 48 hours, enriches their observables with the threat-intelligence integrations it is given, and applies the verdicts. It never decides on its own.
- **Indicator Decay Judge** decides, from the enrichment evidence alone, whether each indicator is still malicious. It runs on the platform routing model, the lightweight model an administrator sets in `Settings > AI Models`, and has no tools.

When the judge finds an indicator still malicious with a confidence of at least 70, the Guardian raises its score. OpenCTI then restarts the decay from the new score, so the indicator is not revoked; the Guardian also adds a note with the evidence and the verdict, and the label `decay-reenriched`. Otherwise nothing changes in OpenCTI and the decay continues. An indicator whose enrichment failed, or whose evidence is not enough to decide, is left alone and reported as degraded, never as kept.

## Set it up

1. Configure a routing model in `Settings > AI Models`. Without one, the judge runs on the platform default model, and its configuration page says so.
2. Give the Guardian its enrichment sources: as an administrator, open the Guardian's `Configuration` tab and bind the threat-intelligence integrations it should query (VirusTotal, GreyNoise, AbuseIPDB, Shodan, urlscan.io, ...). It uses the OpenCTI integration of whoever it runs as.
3. Try it: an administrator runs `Re-check indicators about to decay` from the Guardian's assignments. It runs as that administrator, with their OpenCTI access.
4. Automate it: open `Hourly indicator decay re-check`, choose the account it runs as under `Run As`, then turn it on. It is off on a new install and stays as you leave it across restarts.

## Read the results

Each run reports how many indicators it examined, kept (with their new score), left to decay, and degraded, with the reason. The Guardian keeps one record per indicator in its workset (`Work > Operations > Worksets`), with the evidence and the verdict, and does not judge an indicator again within 24 hours. A degraded indicator is the exception: its first attempt decided nothing, so the Guardian tries it again about 2 hours later instead of 24, if decay has not revoked it by then.

## Limits

- One run examines at most 20 indicators, the ones closest to revocation; the report says when more are waiting. The hourly run picks them up next.
- Enrichment uses the integrations bound to the Guardian, not the enrichment connectors of OpenCTI.
