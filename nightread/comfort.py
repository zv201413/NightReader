"""Optional pixel adjustments for comfortable reading, never OCR proofing."""

PRESETS = {
    "standard": ("标准", (0, 100, 0)),
    "gentle": ("柔和", (0, 85, 0)),
    "clear": ("清晰", (30, 110, 0)),
}


def parameters(cfg):
    preset = PRESETS.get(cfg["comfort_preset"])
    return preset[1] if preset else (cfg["comfort_weight"], cfg["comfort_contrast"],
                                     cfg["comfort_brightness"])


def thicken(base, amount):
    if not amount:
        return base
    import fitz
    import numpy as np
    from PIL import Image, ImageFilter
    result = fitz.Pixmap(base.colorspace, base)
    a = np.frombuffer(result.samples_mv, np.uint8).reshape(result.height, result.width, result.n)
    gray = a[:, :, :3].min(axis=2)
    expanded = np.asarray(Image.fromarray(gray).filter(ImageFilter.MinFilter(3)))
    neutral = a[:, :, :3].max(axis=2) - gray < 16
    target = a[:, :, :3]
    mixed = target.astype(np.float32) * (1 - amount / 100) + expanded[:, :, None] * (amount / 100)
    target[neutral] = np.rint(mixed[neutral]).astype(np.uint8)
    return result


def adjust_tones(pix, contrast, brightness):
    if contrast == 100 and brightness == 0:
        return
    import numpy as np
    table = np.rint(np.clip((np.arange(256) - 127.5) * contrast / 100 +
                           127.5 + brightness, 0, 255)).astype(np.uint8)
    a = np.frombuffer(pix.samples_mv, np.uint8)
    a[:] = table[a]
