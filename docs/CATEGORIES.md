# Configuring event categories

Admin → **Rules & categories → Categories**. Changes take effect immediately; existing event data is kept.

## Field schema (JSON list)

```json
[
  {"key": "batch_name", "label": "Batch name / identifier", "type": "text", "required": true},
  {"key": "programme", "label": "Programme", "type": "select", "required": true,
   "options": ["Civil Judge / Judicial Services", "ADPO / APO / Prosecution", "Other"]},
  {"key": "promo_deadline", "label": "Promotion deadline", "type": "date", "before_event": true},
  {"key": "registrations", "label": "Registrations received", "type": "int", "phase": "execution"}
]
```

| Property | Meaning |
|---|---|
| `key` | Permanent identifier (letters, digits, underscore). Never rename a key that already holds data. |
| `label` | What users see |
| `type` | `text`, `textarea`, `int`, `date`, `time`, `datetime`, `select`, `multiselect`, `bool`, `url` |
| `required` | Required **to submit** (drafts can be incomplete) |
| `phase` | `plan` (default) or `execution` – execution fields appear after approval and are never required to submit |
| `options` | For `select` / `multiselect` |
| `before_event` | For dates: must be on or before the event date |
| `help` | Tooltip text |

## Common-field overrides (JSON object)

Relabel, require or hide the built-in fields per category:

```json
{
  "event_date": {"label": "Planned dispatch date"},
  "end_time":   {"hidden": true},
  "venue":      {"required": true, "label": "Venue or mode"}
}
```

Built-in fields: `title`, `subtype`, `event_date`, `start_time`, `end_time`, `description`, `objective`,
`responsible_person`, `expected_participation`, `target_audience`, `venue`, `preparation_deadline`,
`actual_participation`, `post_event_remarks`. Title and date are always required.
By default `start_time` and `responsible_person` are required on submission.

## Other switches

* **Same-slot clash** – `block` (hard stop when two *block* categories overlap at a centre), `warn`, `none`.
* **Uses a time slot / room** – off for things like WhatsApp campaigns.
* **Informal engagement** – shown with a ☕ marker and separated from formal academic events.
* **Supports national parent** – enables the National events flow (one parent, per-centre records).
* Linking (e.g. WhatsApp → batch launch) is set by `link_target_key` and is currently configured for the
  built-in WhatsApp category.
