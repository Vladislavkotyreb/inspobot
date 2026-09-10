"""Каталог того, что можно попросить у Mobbin.

Опирается на реальные возможности MCP-инструментов, а не на желаемые:

* `search_screens` — отдельные экраны, параметр platform (ios/web);
* `search_flows` — многошаговые сценарии, тоже с platform;
* `search_sections` — секции сайтов, **без** platform: они всегда веб.

Фильтра «B2B/B2C» у Mobbin нет вовсе. Поэтому аудитория здесь — не параметр
поиска, а два разных набора тем со своими формулировками запроса: у B2B
спрашивают про биллинг и роли, у B2C — про пейволл и ленту. Так разделение
получается настоящим, а не декоративным.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Option:
    """Один вариант выбора: код для кнопки, подпись для человека."""

    code: str
    label: str


@dataclass(frozen=True)
class Topic:
    slug: str
    title: str
    query: str


# --- оси выбора -------------------------------------------------------------

KINDS = (
    Option("s", "Экраны"),
    Option("f", "Флоу"),
    Option("w", "Секции сайта"),
)

KIND_HINTS = {
    "s": "отдельные экраны по теме",
    "f": "многошаговые сценарии целиком",
    "w": "блоки лендингов: герой, тарифы, отзывы",
}

PLATFORMS = (
    Option("i", "Мобильные"),
    Option("w", "Десктоп"),
)

PLATFORM_API = {"i": "ios", "w": "web"}

AUDIENCES = (
    Option("b", "B2B"),
    Option("c", "B2C"),
)

# Секции сайта существуют только для веба — платформу спрашивать не у чего.
KINDS_WITHOUT_PLATFORM = {"w"}


# --- темы -------------------------------------------------------------------

SCREENS_B2C = (
    Topic("onboarding", "Онбординг",
          "onboarding flow with personalization questions and a progress indicator"),
    Topic("paywall", "Пейволл",
          "subscription paywall with plan comparison, free trial and pricing toggle"),
    Topic("checkout", "Оплата",
          "checkout screen with saved cards, delivery options and a pay button"),
    Topic("feed", "Лента",
          "content feed with cards, category filters and pull to refresh"),
    Topic("search", "Поиск",
          "search screen with recent queries, filter chips and results list"),
    Topic("profile", "Профиль",
          "user profile screen with avatar, stats and activity history"),
    Topic("detail", "Карточка",
          "product detail screen with hero image, options and a sticky action bar"),
    Topic("empty", "Пустые состояния",
          "empty state screen with illustration, explanation and a primary action"),
    Topic("permissions", "Разрешения",
          "permission request screen explaining why the app needs access"),
    Topic("settings", "Настройки",
          "settings screen with grouped rows, toggles and a destructive action"),
)

SCREENS_B2B = (
    Topic("dashboard", "Дашборд",
          "B2B SaaS analytics dashboard with KPI tiles, charts and a date range picker"),
    Topic("table", "Таблицы",
          "B2B SaaS data table with sortable columns, bulk selection and inline filters"),
    Topic("team", "Команда и роли",
          "B2B SaaS team members page with role selection and an invite dialog"),
    Topic("billing", "Биллинг",
          "B2B SaaS billing page with invoice history, payment method and usage summary"),
    Topic("integrations", "Интеграции",
          "B2B SaaS integrations page with connected services and connection status"),
    Topic("admin", "Админка",
          "B2B SaaS admin settings page with a sidebar of sections and form fields"),
    Topic("filters", "Фильтры и сегменты",
          "B2B SaaS segment builder with condition rules and a live result count"),
    Topic("empty", "Пустые состояния",
          "B2B SaaS empty state with explanation, sample data and a primary action"),
    Topic("editor", "Редактор",
          "B2B SaaS editor interface with a canvas, layers panel and properties panel"),
    Topic("inbox", "Инбокс",
          "B2B SaaS inbox with a message list, thread view and reply composer"),
)

FLOWS_B2C = (
    Topic("signup", "Регистрация",
          "sign up with email verification and profile setup steps"),
    Topic("onboarding", "Онбординг",
          "onboarding with personalization questions leading to a first result"),
    Topic("purchase", "Покупка",
          "checkout with delivery, payment method selection and order confirmation"),
    Topic("subscribe", "Подписка",
          "subscribing to a paid plan from paywall to payment confirmation"),
    Topic("addcard", "Привязка карты",
          "adding a payment card with scanning and confirmation steps"),
    Topic("recover", "Восстановление доступа",
          "password recovery from request to setting a new password"),
    Topic("booking", "Бронирование",
          "booking a slot with date selection, details and confirmation"),
    Topic("kyc", "Верификация",
          "identity verification with document capture and selfie steps"),
)

FLOWS_B2B = (
    Topic("signup", "Регистрация компании",
          "B2B SaaS sign up with company details and workspace creation"),
    Topic("invite", "Приглашение команды",
          "inviting teammates to a workspace and assigning roles"),
    Topic("setup", "Настройка продукта",
          "B2B SaaS setup wizard with numbered steps and a progress sidebar"),
    Topic("integration", "Подключение интеграции",
          "connecting a third party integration with authorization and mapping steps"),
    Topic("upgrade", "Апгрейд тарифа",
          "upgrading a subscription plan with comparison and payment steps"),
    Topic("project", "Создание проекта",
          "creating a new project with template selection and settings"),
    Topic("import", "Импорт данных",
          "importing data from a file with column mapping and validation"),
    Topic("checkout", "Оформление счёта",
          "B2B checkout with billing details, tax information and invoice"),
)

SECTIONS_B2C = (
    Topic("hero", "Герой",
          "hero section with headline, product screenshot and a call to action"),
    Topic("pricing", "Тарифы",
          "pricing section with plan cards and a billing period toggle"),
    Topic("features", "Возможности",
          "features section with icons, short descriptions and screenshots"),
    Topic("testimonials", "Отзывы",
          "testimonials section with customer quotes, photos and ratings"),
    Topic("faq", "FAQ",
          "frequently asked questions section with expandable answers"),
    Topic("gallery", "Галерея",
          "gallery section with product photos and captions"),
    Topic("cta", "Призыв к действию",
          "call to action section with a signup form and supporting copy"),
    Topic("footer", "Подвал",
          "footer with grouped navigation links, social icons and legal text"),
)

SECTIONS_B2B = (
    Topic("hero", "Герой",
          "B2B SaaS hero section with headline, product screenshot and demo request"),
    Topic("pricing", "Тарифы",
          "B2B SaaS pricing section with plan comparison table and enterprise tier"),
    Topic("features", "Возможности",
          "B2B SaaS features section with product screenshots and benefit copy"),
    Topic("logos", "Клиенты",
          "customer logos section with social proof and metrics"),
    Topic("integrations", "Интеграции",
          "integrations section with a grid of connected tools"),
    Topic("security", "Безопасность",
          "security and compliance section with certifications and guarantees"),
    Topic("casestudy", "Кейсы",
          "case study section with customer results and quotes"),
    Topic("cta", "Демо и контакт",
          "book a demo section with a contact form and sales copy"),
)

TOPICS: dict[str, dict[str, tuple[Topic, ...]]] = {
    "s": {"b": SCREENS_B2B, "c": SCREENS_B2C},
    "f": {"b": FLOWS_B2B, "c": FLOWS_B2C},
    "w": {"b": SECTIONS_B2B, "c": SECTIONS_B2C},
}


def topics_for(kind: str, audience: str) -> tuple[Topic, ...]:
    return TOPICS[kind][audience]


def find_topic(kind: str, audience: str, slug: str) -> Topic | None:
    for topic in topics_for(kind, audience):
        if topic.slug == slug:
            return topic
    return None


def label(options: tuple[Option, ...], code: str) -> str:
    for option in options:
        if option.code == code:
            return option.label
    return code
