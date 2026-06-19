import os
import json
import random
import asyncio
import logging
from pathlib import Path
import aiohttp

logging.getLogger('discord').setLevel(logging.WARNING)

import discord
from discord import app_commands, ui
from discord.ext import commands
from discord.webhook.async_ import AsyncWebhookAdapter
from dotenv import load_dotenv
from pinscrape import Pinterest

_request_orig = AsyncWebhookAdapter.request


async def _request_patched(self, route, **kwargs):
    import asyncio
    for attempt in range(5):
        try:
            return await _request_orig(self, route, **kwargs)
        except aiohttp.ClientError as e:
            if attempt < 4:
                await asyncio.sleep(1 + attempt * 2)
                continue
            raise


AsyncWebhookAdapter.request = _request_patched

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    msg = "BOT_TOKEN not found in .env file"
    raise ValueError(msg)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=None, intents=intents)


@bot.event
async def on_ready():
    print(f"{bot.user} is online!")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    bot.loop.create_task(autopost_loop())


def build_simple_image_view(url: str) -> ui.LayoutView:
    v = ui.LayoutView()
    container = ui.Container()
    gallery = ui.MediaGallery()
    gallery.add_item(media=url)
    container.add_item(gallery)
    v.add_item(container)
    return v


def build_pin_view(images: list[str], topic: str, index: int) -> ui.LayoutView:
    v = ui.LayoutView()
    container = ui.Container()
    total = len(images)

    container.add_item(ui.TextDisplay(f"# {topic}"))
    container.add_item(ui.Separator())

    gallery = ui.MediaGallery()
    gallery.add_item(media=images[index])
    container.add_item(gallery)

    container.add_item(ui.Separator())

    row = ui.ActionRow()

    prev_btn = ui.Button(label="◀", style=discord.ButtonStyle.primary, disabled=index == 0)

    async def prev_cb(interaction: discord.Interaction):
        await interaction.response.edit_message(view=build_pin_view(images, topic, index - 1))
    prev_btn.callback = prev_cb
    row.add_item(prev_btn)

    page_btn = ui.Button(label=f"{index + 1}/{total}", style=discord.ButtonStyle.secondary, disabled=True)
    row.add_item(page_btn)

    next_btn = ui.Button(label="▶", style=discord.ButtonStyle.primary, disabled=index == total - 1)

    async def next_cb(interaction: discord.Interaction):
        await interaction.response.edit_message(view=build_pin_view(images, topic, index + 1))
    next_btn.callback = next_cb
    row.add_item(next_btn)

    container.add_item(row)
    v.add_item(container)
    return v


@bot.tree.command(name="pinscrape", description="Search and snipe images from Pinterest")
@app_commands.describe(
    topic="What to search for (e.g. cats)",
    amount="Number of images (1-10)",
)
async def pinterest(interaction: discord.Interaction, topic: str, amount: app_commands.Range[int, 1, 10] = 1):
    await interaction.response.defer()

    p = Pinterest(sleep_time=1)

    try:
        image_urls = p.search(topic, 25)
    except Exception as e:
        await interaction.followup.send(f"Search failed: {e}")
        return

    if not image_urls:
        await interaction.followup.send("No images found.")
        return

    random.shuffle(image_urls)
    selected = [str(u) for u in image_urls[:amount]]

    view = build_pin_view(selected, topic, 0)
    await interaction.followup.send(view=view)


FEEDS_FILE = Path("data/autopost_feeds.json")


def load_feeds() -> dict:
    if FEEDS_FILE.exists():
        return json.loads(FEEDS_FILE.read_text())
    return {}


def save_feeds(feeds: dict):
    FEEDS_FILE.parent.mkdir(exist_ok=True)
    FEEDS_FILE.write_text(json.dumps(feeds, indent=2))


@bot.tree.command(name="autopost", description="Send images periodically to a channel")
@app_commands.describe(
    topic="What to search for (e.g. dogs)",
    channel="The channel to send images to",
    interval="Minutes between sends (default: 5)",
)
async def autopost(
    interaction: discord.Interaction,
    topic: str,
    channel: discord.TextChannel,
    interval: app_commands.Range[int, 1, 60] = 5,
):
    feeds = load_feeds()
    channel_id = str(channel.id)
    feeds[channel_id] = {
        "topic": topic,
        "interval": interval,
        "guild_id": interaction.guild_id,
        "last_sent": 0,
    }
    save_feeds(feeds)
    v = ui.LayoutView()
    c = ui.Container()
    c.add_item(ui.TextDisplay(f"Autopost started: sending a {topic} post every {interval} minute in {channel.mention}"))
    v.add_item(c)
    await interaction.response.send_message(view=v, ephemeral=True)


async def autopost_channel_autocomplete(interaction: discord.Interaction, current: str):
    feeds = load_feeds()
    choices = []
    for ch_id, cfg in feeds.items():
        if cfg.get("guild_id") != interaction.guild_id:
            continue
        channel = interaction.guild.get_channel(int(ch_id))
        if channel and current.lower() in channel.name.lower():
            choices.append(app_commands.Choice(name=f"#{channel.name} ({cfg['topic']})", value=str(channel.id)))
    return choices[:25]


@bot.tree.command(name="autopost-stop", description="Stop autopost in a channel")
@app_commands.describe(channel="The channel to stop")
@app_commands.autocomplete(channel=autopost_channel_autocomplete)
async def autopost_stop(interaction: discord.Interaction, channel: str):
    feeds = load_feeds()
    channel_id = channel
    channel_obj = interaction.guild.get_channel(int(channel_id))
    v = ui.LayoutView()
    c = ui.Container()
    if channel_id in feeds:
        del feeds[channel_id]
        save_feeds(feeds)
        c.add_item(ui.TextDisplay(f"Autopost stopped in {channel_obj.mention if channel_obj else '#deleted-channel'}"))
    else:
        c.add_item(ui.TextDisplay("No autopost running in that channel."))
    v.add_item(c)
    await interaction.response.send_message(view=v, ephemeral=True)


async def autopost_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        feeds = load_feeds()
        now = asyncio.get_event_loop().time()
        changed = False

        for channel_id_str, cfg in feeds.items():
            interval_sec = cfg["interval"] * 60
            if now - cfg["last_sent"] < interval_sec:
                continue

            channel = bot.get_channel(int(channel_id_str))
            if not channel:
                continue

            try:
                p = Pinterest(sleep_time=1)
                urls = p.search(cfg["topic"], 25)
                if urls:
                    random.shuffle(urls)
                    view = build_simple_image_view(str(urls[0]))
                    await channel.send(view=view)
                    cfg["last_sent"] = now
                    changed = True
            except Exception as e:
                print(f"Autopost error in {channel_id_str}: {e}")

        if changed:
            save_feeds(feeds)

        await asyncio.sleep(60)


bot.run(BOT_TOKEN)
