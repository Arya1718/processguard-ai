
<!--
source_type: illustrative
source_note: Authored for this ProcessGuard AI case study - modeled on real industrial operational patterns but NOT sourced from Buckman internally; Buckman/Ackumen operational data is proprietary and was never used.
-->
# SOP-TEMP-009: Heat Exchanger Temperature Excursion

## Purpose

Handle rising supply temperature on a cooling loop when flow and vibration
are normal -- a heat-rejection problem, not a pump-mechanics problem.

## Normal operating range

Supply temperature 29-32 degC at design flow (118-132 m3/h). Vibration
below 2.5 mm/s and stable conductivity indicate the pump side is healthy.

## Symptoms of a heat-exchanger problem

- Temperature climbs above 32 degC over 15-60 minutes.
- Flow stays inside its normal band; vibration stays quiet.
- Conductivity may rise slightly with evaporation losses but stays near band.

## Response procedure

1. Check the exchanger outlet valve position; a drifting actuator is the
   usual cause of a slow temperature rise with normal flow.
2. Inspect the exchanger tubes for fouling; backwash if the approach
   temperature exceeds the posted limit.
3. Verify cooling-tower fan operation; failed fans raise the wet-bulb
   ceiling and the loop temperature with it.
4. Reduce process load if temperature exceeds 38 degC.

## Escalation

If temperature exceeds 40 degC with normal flow, treat it as a loss of heat
rejection and escalate to the shift engineer immediately.

## References

- Cooling-tower performance curve set, Site 12.
- SOP-COOL-014 if flow or vibration also leaves band.
