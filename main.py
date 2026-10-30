
import os
import re
import html
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

import feedparser
from bs4 import BeautifulSoup
import requests
from dotenv import load_dotenv
from google import genai
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

api_key = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=api_key) if api_key else None

app = FastAPI(title="Mousy AI Companion")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str


@app.get("/")
def home():
    return {
        "status": "success",
        "message": "Mousy AI Backend Running",
    }


def make_response(reply, sources=None, state="speaking", **extra):
    return {
        "reply": reply,
        "avatar_state": state,
        "sources": sources or [],
        **extra,
    }


def is_weather_question(message):
    text = message.lower()
    return any(word in text for word in [
        "weather", "temperature", "forecast",
        "rain today", "raining", "humidity",
    ])


def is_news_question(message):
    text = message.lower()
    return any(word in text for word in [
        "news", "headlines", "breaking news",
        "latest update", "latest updates",
        "current affairs",
    ])


def extract_location(message, default="Hyderabad"):
    match = re.search(
        r"\b(?:in|at|for|of|about)\s+"
        r"([a-zA-Z][a-zA-Z\s.-]{1,60})",
        message,
        re.IGNORECASE,
    )

    if match:
        location = match.group(1).strip(" .?!")
        location = re.sub(
            r"\b(today|right now|currently|tomorrow|"
            r"this week|latest|news|weather|updates)\b.*$",
            "",
            location,
            flags=re.IGNORECASE,
        ).strip()

        if location:
            return location

    for place in [
        "Andhra Pradesh", "Hyderabad", "Telangana",
        "Vijayawada", "Tirupati", "Bengaluru",
        "Chennai", "Mumbai", "Delhi", "India",
        "London", "New York",
    ]:
        if place.lower() in message.lower():
            return place

    return default


def get_weather(location):
    geo_response = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={
            "name": location,
            "count": 1,
            "language": "en",
            "format": "json",
        },
        timeout=12,
    )
    geo_response.raise_for_status()

    locations = geo_response.json().get("results", [])

    if not locations:
        return None

    place = locations[0]

    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": (
                "temperature_2m,relative_humidity_2m,"
                "apparent_temperature,precipitation,"
                "weather_code,wind_speed_10m"
            ),
            "timezone": "auto",
        },
        timeout=12,
    )
    response.raise_for_status()

    current = response.json().get("current", {})

    codes = {
        0: "clear skies",
        1: "mainly clear skies",
        2: "partly cloudy skies",
        3: "cloudy skies",
        45: "fog",
        48: "fog",
        51: "light drizzle",
        53: "drizzle",
        55: "heavy drizzle",
        61: "light rain",
        63: "moderate rain",
        65: "heavy rain",
        71: "light snow",
        73: "snow",
        75: "heavy snow",
        80: "light rain showers",
        81: "rain showers",
        82: "heavy rain showers",
        95: "thunderstorms",
        96: "thunderstorms with hail",
        99: "thunderstorms with hail",
    }

    description = codes.get(
        current.get("weather_code"),
        "mixed weather conditions",
    )

    name = place.get("name", location)
    country = place.get("country", "")

    reply = (
        f"The current weather in {name}, {country}, "
        f"is {current.get('temperature_2m')} degrees Celsius "
        f"with {description}. "
        f"It feels like {current.get('apparent_temperature')} "
        f"degrees. Humidity is "
        f"{current.get('relative_humidity_2m')} percent."
    )

    return {
        "reply": reply,
        "sources": [{
            "title": "Open-Meteo Weather",
            "url": "https://open-meteo.com/",
        }],
        "weather_observed_at": current.get("time"),
    }


def get_article_image(entry):
    # First check RSS thumbnails
    for field in ("media_thumbnail", "media_content"):
        for media in entry.get(field, []):
            url = media.get("url", "")
            if url.startswith("https://"):
                return url

    # Check RSS image enclosures
    for enclosure in entry.get("enclosures", []):
        url = enclosure.get("href", "")
        if (
            url.startswith("https://")
            and enclosure.get("type", "").startswith("image/")
        ):
            return url

    # Check images in the RSS description
    description = entry.get("summary", "")
    match = re.search(
        r'<img[^>]+src=["\'](https://[^"\']+)',
        description,
        re.IGNORECASE,
    )

    if match:
        return html.unescape(match.group(1))

    # Try to find the real article preview image
    article_url = entry.get("link", "")

    if article_url.startswith("https://"):
        try:
            response = requests.get(
                article_url,
                timeout=5,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()

            soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            image = soup.find(
                "meta",
                property="og:image",
            )

            if image and image.get("content"):
                url = image["content"]

                if url.startswith("https://"):
                    return url

        except requests.RequestException:
            pass

    return ""

def get_news(location):
    query = f"{location} news when:2d"

    url = (
        "https://news.google.com/rss/search?"
        + urlencode({
            "q": query,
            "hl": "en-IN",
            "gl": "IN",
            "ceid": "IN:en",
        })
    )

    response = requests.get(
        url,
        timeout=12,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()

    feed = feedparser.parse(response.content)
    articles = []

    for entry in feed.entries:
        title = html.unescape(
            re.sub(r"<[^>]*>", "", entry.get("title", ""))
        ).strip()

        link = entry.get("link", "").strip()

        if not title or not link:
            continue

        articles.append({
            "title": title,
            "url": link,
            "published": entry.get("published", ""),
            "image_url": get_article_image(entry),
        })

        if len(articles) >= 5:
            break

    return articles


def make_news_reply(location, articles):
    if not articles:
        return (
            f"I couldn't find recent news for {location} "
            "right now. Please try again later."
        )

    reply = f"Here are some recent headlines about {location}. "

    for number, article in enumerate(articles[:3], start=1):
        reply += f"Headline {number}: {article['title']}. "

    reply += "You can check the news sources for full details."
    return reply


@app.post("/api/chat")
def chat(request: ChatRequest):
    message = request.message.strip()

    if not message:
        return make_response(
            "Please say something.",
            state="idle",
        )

    if is_weather_question(message):
        location = extract_location(message)

        if location.lower() == "andhra pradesh":
            location = "Vijayawada"

        try:
            result = get_weather(location)

            if result:
                return make_response(
                    result["reply"],
                    sources=result["sources"],
                    weather_observed_at=result["weather_observed_at"],
                )

            return make_response(
                f"I couldn't find weather information for {location}. "
                "Please try saying the city name."
            )

        except Exception:
            logger.exception("Weather retrieval failed")
            return make_response(
                "Sorry, live weather is temporarily unavailable."
            )

    if is_news_question(message):
        location = extract_location(message)

        try:
            articles = get_news(location)

            return make_response(
                make_news_reply(location, articles),
                sources=articles,
                news_checked_at=datetime.now(
                    timezone.utc
                ).isoformat(),
            )

        except Exception:
            logger.exception("News retrieval failed")
            return make_response(
                "Sorry, I couldn't retrieve the latest news "
                "right now. Please try again later."
            )

    if gemini_client is None:
        return make_response(
            "Gemini API key is not configured.",
            state="idle",
        )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=message,
            config={
                "system_instruction": (
                    "You are Mousy, a friendly AI Talking Companion. "
                    "Answer questions naturally in English. "
                    "Be helpful and conversational. "
                    "Keep most answers concise because they "
                    "will be spoken aloud. "
                    "If you do not know something, say so. "
                    "Do not invent current facts, news, or weather."
                )
            },
        )

        reply = (
            response.text
            or "Sorry, I couldn't generate a reply."
        )

        return make_response(reply)

    except Exception as e:
        logger.error("Gemini error: %s", e)

        return make_response(
            "Sorry, I am having trouble connecting "
            "to AI right now.",
            state="idle",
        )
