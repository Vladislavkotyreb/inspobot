"""Ротация тем: каждый день — своя пара «мобильная тема / десктопная тема».

Логика чистая (дата на входе, тема на выходе), поэтому проверяется тестами
без сети, Telegram и Anthropic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Списки намеренно разной длины (14 и 15): будь они равны, любое смещение
# давало бы одну и ту же пару «мобильная тема + десктопная» каждые две
# недели. При разной длине пара повторяется раз в 210 дней. Смещение ниже
# просто сдвигает начало десктопного цикла, чтобы первый день не совпадал.
DESKTOP_OFFSET = 5


@dataclass(frozen=True)
class Topic:
    slug: str
    title: str
    query: str


MOBILE_TOPICS: tuple[Topic, ...] = (
    Topic("onboarding", "Онбординг",
          "onboarding flow with personalization questions and a progress indicator"),
    Topic("paywall", "Пейволл и подписка",
          "subscription paywall with plan comparison, free trial and pricing toggle"),
    Topic("empty-state", "Пустые состояния",
          "empty state screen with illustration, explanation and a primary action"),
    Topic("settings", "Настройки",
          "settings screen with grouped rows, toggles and a destructive action"),
    Topic("search", "Поиск и фильтры",
          "search screen with recent queries, filter chips and results list"),
    Topic("profile", "Профиль",
          "user profile screen with avatar, stats and activity history"),
    Topic("checkout", "Оплата",
          "checkout screen with saved cards, address selection and Apple Pay button"),
    Topic("home-dashboard", "Главный экран",
          "home dashboard with summary cards, metrics and quick actions"),
    Topic("charts", "Графики и статистика",
          "analytics screen with charts, period selector and comparison values"),
    Topic("feed", "Лента и списки",
          "content feed with cards, category filters and pull to refresh"),
    Topic("forms", "Формы и валидация",
          "multi-step form with inline validation errors and helper text"),
    Topic("permissions", "Разрешения и запросы",
          "permission request screen explaining why the app needs access"),
    Topic("detail", "Карточка объекта",
          "item detail screen with hero image, tabs and a sticky bottom action bar"),
    Topic("sheets", "Шторки и модалки",
          "bottom sheet with a list of options and a confirmation action"),
)

DESKTOP_TOPICS: tuple[Topic, ...] = (
    Topic("landing", "Лендинг и герой",
          "landing page hero section with headline, product screenshot and signup form"),
    Topic("pricing", "Тарифы",
          "pricing page with plan comparison table and billing period toggle"),
    Topic("dashboard", "Дашборд",
          "analytics dashboard with charts, KPI tiles and a date range picker"),
    Topic("data-table", "Таблицы и данные",
          "data table with sortable columns, bulk selection and inline filters"),
    Topic("admin-settings", "Настройки продукта",
          "application settings page with a sidebar of sections and form fields"),
    Topic("auth", "Вход и регистрация",
          "sign up page with social login options and email verification step"),
    Topic("setup-wizard", "Мастер настройки",
          "product setup wizard with numbered steps and a progress sidebar"),
    Topic("editor", "Редактор и холст",
          "editor interface with a canvas, left layers panel and right properties panel"),
    Topic("search-web", "Поиск по продукту",
          "search results page with faceted filters and result previews"),
    Topic("billing", "Биллинг и счета",
          "billing page with invoice history, payment method and usage summary"),
    Topic("team", "Команда и доступы",
          "team members page with role selection and invite dialog"),
    Topic("docs", "Документация и база знаний",
          "documentation page with sidebar navigation, code samples and search"),
    Topic("inbox", "Инбокс и переписка",
          "inbox interface with a message list, thread view and reply composer"),
    Topic("empty-error", "Пустые состояния и ошибки",
          "empty state and error page with explanation and recovery action"),
    Topic("dialogs", "Диалоги и подтверждения",
          "confirmation dialog with a destructive action and a clear explanation"),
)


def _pick(topics: tuple[Topic, ...], index: int) -> Topic:
    return topics[index % len(topics)]


def topics_for(day: date) -> tuple[Topic, Topic]:
    """Пара тем дня: (мобильная, десктопная).

    Ротация по порядковому номеру дня, а не по дню недели: цикл длиннее недели,
    так что подряд идущие понедельники не приносят одну и ту же тему.
    """
    ordinal = day.toordinal()
    return (
        _pick(MOBILE_TOPICS, ordinal),
        _pick(DESKTOP_TOPICS, ordinal + DESKTOP_OFFSET),
    )
