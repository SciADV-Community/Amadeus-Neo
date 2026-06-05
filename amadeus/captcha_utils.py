import asyncio
import secrets

import discord
from captcha.image import ImageCaptcha

from amadeus.constants import CAPTCHA_LENGTH


class CaptchaService:
    """
    Generates CAPTCHA text and image files.
    """

    def __init__(self):
        self.generator = ImageCaptcha(width=280, height=90)

    def make_captcha_text(self) -> str:
        """
        Generates a random CAPTCHA code.

        Confusing characters are removed:
        - 0 and O
        - 1 and I
        """

        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

        return "".join(
            secrets.choice(alphabet)
            for _ in range(CAPTCHA_LENGTH)
        )

    async def make_captcha_file(self, captcha_text: str) -> discord.File:
        """
        Generates a CAPTCHA image and turns it into a Discord file.

        Image generation is CPU-bound and runs in a thread pool to avoid
        blocking the event loop under burst load.
        """

        image_data = await asyncio.to_thread(self.generator.generate, captcha_text)
        image_data.seek(0)

        return discord.File(
            fp=image_data,
            filename="captcha.png",
        )


def normalize_code(code: str) -> str:
    """
    Makes CAPTCHA answers case-insensitive.
    """

    return code.strip().upper()