# Future Development Ideas

This file collects ideas that are useful for explaining possible extensions of SprintConnect, but are not part of the current working prototype.

The main source code should stay focused on implemented functionality.

## Scenario Comparison

A future version could compare multiple transfer scenarios for the same connection:

- best case
- realistic case
- worst case

Example interpretation:

- Best case: passenger is near the door, station is known, crowd is low, no stairs delay.
- Worst case: passenger is far from the door, crowd is high, stairs are busy, orientation takes longer.

This could be shown in the GUI as a comparison table:

| Scenario | Required transfer time | Status |
| --- | --- | --- |
| Best case | short | safe |
| Realistic case | medium | tight |
| Worst case | long | critical |

## Walking Distance Model

The project task mentions measuring real transfer times at stations. A later version could use measured distances between platform areas.

Possible formula:

```text
required_transfer_time =
    walking_distance / walking_speed
    + reaction_delay
    + orientation_delay
    + crowd_delay
    + stairs_delay
    + door_closing_buffer
```

This would make the model more realistic than using only GTFS `transfers.txt`.

## Station-Specific Transfer Data

A future extension could store measured transfer times for important stations:

- Zurich HB
- Winterthur
- Zurich Oerlikon
- Zurich Flughafen
- Stettbach

Possible data:

- same platform transfer time
- neighbouring platform transfer time
- underpass transfer time
- platform segment information
- crowd condition observations

## Realtime Data

The current prototype uses static GTFS schedule data.

A future version could include GTFS-Realtime data:

- delays
- cancelled trips
- changed platforms
- live connection risk updates

## GUI Improvements

Possible GUI extensions:

- station autocomplete
- route selection as cards or table rows
- map or station plan view
- color legend for risk labels
- export selected connection as a short report

## Useful Links

- ZVV timetable: https://www.zvv.ch/en/timetable-and-information/timetable.html?tab=connections
- ZVV network information: https://www.zvv.ch/en/timetable-and-information/network.html
- Open Data Platform Mobility Switzerland: https://data.opentransportdata.swiss

