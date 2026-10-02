# voicectl

Push-to-talk dictation that types into whatever window has focus. Hold a key,
talk, let go — the text appears where your cursor is.

## Why

Claude Code has a `/voice` command, but it captures audio in the process that
runs it. Over SSH that process is on the *remote* machine, so it goes looking
for a microphone that isn't there. It's also unavailable on some backends.

Injecting text at the compositor level instead sidesteps both. The keystrokes
land in the local terminal, which ships them over SSH as ordinary input — the
remote end sees characters arrive on stdin and can't tell them from typing.
Nothing is installed on the far side, and the same mechanism works in a
browser, a chat window, or a commit message. Transcription is local, so audio
never leaves the machine.

## How it works

```
pw-record ──> ring buffer ──> Moonshine ──> substitutions ──> primary selection
 (always)      (15s)          (while armed)                        │
                                                            wtype Shift+Insert
                                                                   │
                                                            focused window
```

Two choices carry most of the weight:

**The capture stream never closes.** Opening an audio device costs a few hundred
milliseconds and the mic has to wake from suspend, which is why press-to-record
swallows your first word. Here the device is already awake, and a rolling buffer
means the ~1s *before* the key went down gets transcribed too. You can start
talking as you press, or slightly ahead of it.

**Moonshine decodes while you talk.** It's a streaming model, so releasing the
key doesn't start any transcription work — measured decode tail is ~2ms, against
the second or more a batch model would spend. It runs entirely on CPU at ~5x
realtime, so the GPU is irrelevant.

End to end, release-to-paste is that 2ms plus `postroll_ms` (400 by default),
because capture runs behind real time and the tail of the sentence is still in
flight when the key comes up. The wait is for audio to arrive, not for the model
to think.

**Text goes through the primary selection, not the clipboard.** Primary is
already volatile — selecting any text overwrites it — so nothing depends on it
persisting, whereas clobbering Ctrl+C would be intolerable. The window between
writing it and pasting it is milliseconds.

## What this was built on

This is one person's setup, working. Everything below is an assumption you may
need to change — see [Adapting this](#adapting-this).

| | |
| :--- | :--- |
| OS | Arch Linux |
| Audio | PipeWire |
| Compositor | [mango](https://github.com/DreamMaoMao/mangowc) (wlroots/dwl-derived), Wayland |
| Terminal | kitty |
| Keyboard | Dvorak |
| CPU | Ryzen 5 7600 — all timings below are from this |

## Requirements

```
# Arch; adjust for your distro
sudo pacman -S --needed wtype wl-clipboard

uv venv && uv pip install moonshine-voice
.venv/bin/moonshine download --stt --language en --model-arch 5   # 5 = MEDIUM_STREAMING
```

`pw-record` ships with PipeWire. Wayland only — on X11 you'd swap `wl-copy` and
`wtype` for `xclip` and `xdotool`.

Then link the service and reload:

```
ln -s "$PWD/voicectl.service" ~/.config/systemd/user/voicectl.service
systemctl --user daemon-reload
```

## Usage

```
systemctl --user start voicectl     # opens the mic
systemctl --user stop voicectl      # releases it
```

Deliberately not enabled at boot. Measured cost:

| State | |
| :--- | :--- |
| Idle | 0.10% of one core |
| Armed, not speaking | 0.9% of one core — VAD gates the decoder |
| Armed, speaking | Over a core's worth; onnxruntime decodes multi-threaded |
| Resident | ~943 MB — the model loads at startup, not on first use |

CPU is effectively free and memory is the price. That's the deal that buys the
0.5s model load being paid once rather than on every keypress. `small-streaming`
roughly halves the footprint at 7.84% WER instead of 6.65%, if you want it.

None of that is why it doesn't start at boot, though — it holds the microphone
open the whole time it runs, and that should be a decision you make rather than
a default. PipeWire shares capture devices fine, so it coexists with a call or a
game; the reason to stop it is the rolling buffer, not contention.

| Command | Effect |
| :--- | :--- |
| `voicectl start` | Begin listening (includes the pre-roll) |
| `voicectl stop` | Stop, transcribe, paste into the focused window |
| `voicectl cancel` | Stop and discard |
| `voicectl last` | Re-paste the last transcript |
| `voicectl retro [secs]` | Transcribe the last N seconds already in the buffer |
| `voicectl status` | Whether it's listening, and how much audio has passed through |

`retro` is why the ring holds 15 seconds when push-to-talk needs one: it catches
the thing you already said out loud.

## Keybindings

Push-to-talk needs a compositor that can trigger on key *release*, which is the
main portability constraint. In mango that's the `r` flag:

```ini
# Hold Right Ctrl to dictate
binds=none,Control_R,spawn,/path/to/voicectl start
bindsr=ctrl,Control_R,spawn,/path/to/voicectl stop
binds=SUPER,Escape,spawn,/path/to/voicectl cancel
binds=SUPER+SHIFT,Escape,spawn,/path/to/voicectl retro 15
```

The asymmetry is not a typo. On press the modifier isn't registered as held yet,
so it matches `none`; on release it is, so it matches `ctrl`. The `s` flag is
keysym rather than keycode matching, which this config uses throughout because
of Dvorak.

Note the absence of mango's `p` (pass-through) flag: the compositor consumes
Right Ctrl entirely, so the focused application never sees Ctrl held. If it did,
the Shift+Insert paste would arrive as Ctrl+Shift+Insert and hit a different
binding.

Right Ctrl is just a key nothing else claimed. A dedicated key is better if you
have one — `F13`–`F24` exist in the keymap but no physical keyboard ships them,
so a macropad sending those can't collide with anything.

## Adapting this

The likely friction points, roughly in order:

**Release-triggered keybinds.** Sway has `bindsym --release`, Hyprland has
`bindr`, mango has the `r` flag. If yours can't do it, use a toggle instead —
tap to start, tap to stop — which is arguably better ergonomics for long
dictation anyway. Only `start` and `stop` need to reach the daemon; how you
trigger them is up to you.

**The paste keystroke.** `wtype -M shift -k Insert -m shift` assumes your
terminal maps Shift+Insert to paste-from-primary. kitty does by default, as do
most terminals following the xterm convention. GUI applications (Firefox,
Slack, ...) paste the *clipboard* on Shift+Insert and only read primary on
middle-click, which lands at the pointer rather than the caret. So the daemon
asks mango which window has focus (`mmsg get focusing-client`) and, unless the
app_id is in `paste_keystroke_apps`, types the text out with `wtype -` instead.
If `mmsg` isn't there or fails, it falls back to the keystroke. On another
compositor, swap `Injector.focused_appid` for whatever yours offers.

**Keysym vs keycode.** The `s` flag exists here because of Dvorak. On QWERTY you
probably don't need it.

**Microphone.** `source = ""` uses your default input. Set it explicitly if you
have several and don't want the choice left to chance.

**Paths.** The service unit assumes `~/projects/voicectl`. systemd has no
specifier for "wherever this unit lives", so edit `ExecStart` if you cloned
elsewhere.

## Configuration

`config.toml`. The parts worth touching:

- `preroll_ms` — how far back to reach when the key goes down. 1000 is generous;
  lower it if you pick up audio from before you meant to start.
- `postroll_ms` — how long to keep collecting after the key comes up. Without
  this the last word gets truncated, because capture runs behind real time. It
  also forgives releasing the key slightly early. This is the latency/safety
  dial: lower it if the paste feels sluggish, raise it if words go missing.
- `line_join` — `" "` keeps a transcript on one line so a newline can never
  submit a prompt early. `"\n"` preserves breaks for long-form dictation.
- `suffix` — appended to every paste. Defaults to a space so consecutive
  dictations don't run together into oneword.
- `auto_paste` — false loads the primary selection without synthesizing the
  keystroke, leaving you to paste manually.
- `paste_keystroke_apps` — focused app_ids that get Shift+Insert; everything
  else gets typed out. Add your terminal if it isn't kitty.
- `[substitutions]` — Moonshine has no vocabulary biasing, so jargon it reliably
  mangles gets corrected here. Whole-word, case-insensitive, longest match
  first. The shipped entries are examples; replace them with your own.

## Notes

Moonshine drops disfluencies on its own — "let's review, uhh, PR 370" comes out
clean — so there's no cleanup pass to run.

It normalizes aggressively in both directions, though: that same instinct turns
"PR 370" into "pr370". Harmless when an LLM is reading it, worth a substitution
entry when it isn't.

To hear exactly what the model heard, add `save_input_wav_path` to
`[model.options]`.

## Not built, deliberately

**Voice command mode** — a second trigger that synthesizes keystrokes instead of
pasting text, so a numbered prompt could be answered by speaking. If it does get
built, the design is already decided:

**Spoken numbers to number keys only. Never "approve" → `1`.** Option meanings
shift between prompts — `2` is sometimes "no" and sometimes "yes, and stop
asking", which is a standing permission grant. A semantic mapping can therefore
do the wrong thing silently and irreversibly. Numbers keep the interpretation
with the human, and generalize to any numbered prompt, including multi-step
ones, with no table to re-teach.

**Refuse to guess.** Act only on a clean single number token; do nothing
otherwise. A bad transcript is visible text you can delete, but a synthesized
keypress is an action you cannot. Watch homophones — "two/to/too", "four/for",
"one/won" — and note Moonshine sometimes emits digits directly, so both
spellings need handling.

`escape` and `enter` are safe to include: they're structural (get out, confirm
what's highlighted) rather than semantic, so they carry none of the risk.

Moonshine ships an `IntentRecognizer` for semantic matching. It's **not** needed
here — plain phrase matching on the transcript suffices, and the recognizer
costs another ~300 MB model (`embeddinggemma-300m`) plus a dependency on a
module whose own docstring marks it internal.

A hardware macropad retires this feature entirely, which is a decent sign it was
never the right shape.

## Layout

| File | |
| :--- | :--- |
| `voicectld.py` | The daemon |
| `voicectl` | Client; what the keybindings call |
| `config.toml` | Settings and substitutions |
| `voicectl.service` | systemd user unit |
| `bench.py` | Times a WAV through the model — load, realtime factor, tail latency |

## License

MIT
