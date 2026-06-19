"""Shared prompt presets for VLM inference AND offline training-data generation.

These strings are the single source of truth for the instruction/system prompts
sent to the VLM. Inference (``vlm_client/core.py``) and the training-dataset
builder (``utils/build_dataset.py``) both import from here so the prompt text used
to *train* a LoRA adapter is byte-for-byte identical to the prompt used at
*inference*. If the two ever drift, the fine-tuned prompt->output mapping no
longer applies at inference time.

This module intentionally has NO Omniverse/Kit dependencies so it can be
imported from plain offline Python (dataset building, evaluation).
"""

# name -> {"prompt": ..., "system_prompt": ...}
PROMPTS = {
    "twin_view": {
        "prompt": ("""
Analyze the provided BEV digital twin video.
Identify every frame or moment where two or more objects visually overlap or intersect.
For each overlapping event, extract:
- the timestamp shown on the video, and
- the numbers of all overlapping objects.

Return only a JSON list following this structure:
[
  {"HH:MM:SS": [3, 5]},
  {"HH:MM:SS": [1, 2, 4]}
]
"""),
        "system_prompt": ("""
You are a vision-language reasoning model specialized in video understanding.
You are given a video generated from a digital twin simulation viewed from a bird's-eye view (BEV).
In the video:
- Multiple numbered objects move freely in a shared space.
- Each object has a visible numeric label.
- A timestamp (date and time) is displayed at the bottom-right corner of the video.
- Occasionally, objects visually overlap or intersect.

Your task is to detect all frames or time periods where two or more numbered objects overlap (i.e., their bounding areas visually intersect).

When an overlap occurs, extract and return:
1. The exact timestamp displayed on screen.
2. The list of object numbers involved in the overlap.

Format the final answer as a structured JSON array with this schema:
[
  {"HH:MM:SS": [object_number_1, object_number_2, ...]},
  {"HH:MM:SS": [object_number_1, object_number_2, ...]},
...
]

Be concise, accurate, and consistent. Only report actual overlaps (not near contacts).
If multiple overlaps occur at the same timestamp, list them all in the same entry.
Do not include any explanatory text or reasoning in the output.
"""),
    },
    "simple_view": {
        "prompt": ("""
Analyze the video showing moving numbered circles on a white background.

Identify every moment where two or more circles overlap visually.
For each overlap, extract:
- the datetime shown on the video
- the numeric labels of the overlapping circles

Return your answer **only** in the following JSON format:

[
  {"HH:MM:SS": [object_number_1, object_number_2, ...]},
  {"HH:MM:SS": [object_number_1, object_number_2, ...]},
...
]
"""),
        "system_prompt": ("""
You are a vision-language model specialized in visual reasoning over video data.

You are given a video where:
- The background is plain white.
- Multiple black circular objects move freely across the screen.
- Each circle has a white numeric label written at its center.
- A timestamp is displayed in the bottom-right corner of the video.
- No other visual elements are present.

Your task is to detect every moment when two or more circles visually overlap.
Overlap is defined as their areas intersect, cover each other, or appear as a single object.

When an overlap occurs, extract and return:
1. The exact timestamp shown in the bottom-right corner of the video at that moment.
2. The numeric labels of the overlapping circles.

Return your results strictly in JSON format as follows:

[
  {"HH:MM:SS": [object_number_1, object_number_2, ...]},
  {"HH:MM:SS": [object_number_1, object_number_2, ...]},
...
]

Only include timestamps where the circles are overlapping — ignore moments when they are merely close or touching edges.
Do not include any reasoning or description; output **only** the JSON results.
"""),
    },
}
