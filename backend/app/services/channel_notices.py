# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Notices the channel bots send, in the reader's language.

A notice is the platform's own sentence on a messaging channel (Slack, Teams,
WhatsApp, Telegram, Discord, Google Chat): a message queued, a refusal, an
error fallback, a continuation opening, the label of a link to a file, deck
or report with no name - never what an agent wrote.
:data:`NOTICES` holds each one in English, Spanish, French and Japanese, with
``{name}`` placeholders; :func:`notice` renders one, falling back to English
for a language or a translation the catalog does not hold.

The language is the reader's, never guessed from a message
(:func:`notice_language`): an identified platform user's own preference, else
the platform default language, else English. A sender the platform has not
identified - an anonymous one, or one a gate answers before the sender is
looked up - gets the platform default.

Services shared with the web chat (the quota, the prompt guards, the agent
loop and its per-run budget, the approval gate, assignment notifications) keep
writing their notices in English into what they store. :func:`localized_reply`
translates such a text on its way out to a channel by matching the English
template - the whole reply, or the budget notice closing an answer the budget
cut (:data:`TRAILING_NOTICES`) - so the stored conversation, and every
comparison made against it, is unchanged.
"""

from __future__ import annotations

import functools
import re
import string
import uuid
from typing import Any

from app.core.utils import LANGUAGE_AUTO, normalize_language

LANGUAGES: tuple[str, ...] = ("en", "es", "fr", "ja")
DEFAULT_LANGUAGE = "en"

NOTICES: dict[str, dict[str, str]] = {
    # --- The channel router -------------------------------------------------
    "processing_error": {
        "en": "I encountered an error while processing your message. Please try again.",
        "es": "Se produjo un error al procesar su mensaje. Vuelva a intentarlo.",
        "fr": "Une erreur s'est produite lors du traitement de votre message. Veuillez réessayer.",
        "ja": "メッセージの処理中にエラーが発生しました。もう一度お試しください。",
    },
    "injection_refused": {
        "en": "Sorry, I can't process that message \u2014 it looks like an attempt to override my instructions. Please rephrase your request.",
        "es": "Lo siento, no puedo procesar ese mensaje: parece un intento de anular mis instrucciones. Reformule su solicitud.",
        "fr": "Désolé, je ne peux pas traiter ce message : il ressemble à une tentative de contourner mes instructions. Veuillez reformuler votre demande.",
        "ja": "申し訳ありません。このメッセージは処理できません。私の指示を上書きしようとする試みのようです。リクエストを言い換えてください。",
    },
    "reset_done": {
        "en": "Conversation reset! Your next message will start a fresh session.",
        "es": "Conversación restablecida. Su próximo mensaje iniciará una sesión nueva.",
        "fr": "Conversation réinitialisée ! Votre prochain message démarrera une nouvelle session.",
        "ja": "会話をリセットしました。次のメッセージから新しいセッションが始まります。",
    },
    "reset_nothing": {
        "en": "No active conversation to reset. Your next message will start a new session.",
        "es": "No hay ninguna conversación activa que restablecer. Su próximo mensaje iniciará una sesión nueva.",
        "fr": "Aucune conversation active à réinitialiser. Votre prochain message démarrera une nouvelle session.",
        "ja": "リセットする進行中の会話はありません。次のメッセージから新しいセッションが始まります。",
    },
    "stop_done": {
        "en": "Got it \u2014 I'll stop monitoring this thread. @mention me again if you need me back.",
        "es": "Entendido: dejaré de seguir este hilo. Vuelva a @mencionarme si me necesita.",
        "fr": "Compris : je ne suis plus ce fil. @mentionnez-moi à nouveau si vous avez besoin de moi.",
        "ja": "承知しました。このスレッドの監視を停止します。再び必要なときは @メンションしてください。",
    },
    "stop_threads_only": {
        "en": "This command only works in threads. Use /reset to start a fresh conversation instead.",
        "es": "Este comando solo funciona en hilos. Use /reset para iniciar una conversación nueva.",
        "fr": "Cette commande ne fonctionne que dans les fils. Utilisez /reset pour démarrer une nouvelle conversation.",
        "ja": "このコマンドはスレッド内でのみ使用できます。新しい会話を始めるには /reset を使用してください。",
    },
    "agent_unavailable": {
        "en": "The agent for this conversation is no longer available.",
        "es": "El agente de esta conversación ya no está disponible.",
        "fr": "L'agent de cette conversation n'est plus disponible.",
        "ja": "この会話のエージェントは利用できなくなりました。",
    },
    "agent_disabled": {
        "en": "This agent is currently disabled.",
        "es": "Este agente está desactivado actualmente.",
        "fr": "Cet agent est actuellement désactivé.",
        "ja": "このエージェントは現在無効になっています。",
    },
    "agent_not_on_channel": {
        "en": "This agent is not available on the {channel} channel.",
        "es": "Este agente no está disponible en el canal {channel}.",
        "fr": "Cet agent n'est pas disponible sur le canal {channel}.",
        "ja": "このエージェントは {channel} チャンネルでは利用できません。",
    },
    "no_suitable_agent": {
        "en": "I couldn't find a suitable agent for this channel. Please contact an administrator.",
        "es": "No encontré ningún agente adecuado para este canal. Póngase en contacto con un administrador.",
        "fr": "Je n'ai trouvé aucun agent adapté à ce canal. Veuillez contacter un administrateur.",
        "ja": "このチャンネルに適したエージェントが見つかりませんでした。管理者にお問い合わせください。",
    },
    "assistant_not_on_channel": {
        "en": "The General Assistant is not available on the {channel} channel.",
        "es": "El Asistente general no está disponible en el canal {channel}.",
        "fr": "L'Assistant général n'est pas disponible sur le canal {channel}.",
        "ja": "ジェネラルアシスタントは {channel} チャンネルでは利用できません。",
    },
    "account_unidentified": {
        "en": "I couldn't identify your account.",
        "es": "No pude identificar su cuenta.",
        "fr": "Je n'ai pas pu identifier votre compte.",
        "ja": "アカウントを特定できませんでした。",
    },
    "account_unregistered": {
        "en": "I couldn't identify your account. Please make sure your email is registered in {product}.",
        "es": "No pude identificar su cuenta. Asegúrese de que su correo electrónico esté registrado en {product}.",
        "fr": "Je n'ai pas pu identifier votre compte. Vérifiez que votre adresse e-mail est enregistrée dans {product}.",
        "ja": "アカウントを特定できませんでした。メールアドレスが {product} に登録されていることを確認してください。",
    },
    "conversation_failed": {
        "en": "Something went wrong creating the conversation.",
        "es": "Algo salió mal al crear la conversación.",
        "fr": "Un problème est survenu lors de la création de la conversation.",
        "ja": "会話の作成中に問題が発生しました。",
    },
    "ai_disabled": {
        "en": "AI is disabled on this platform, so I cannot answer right now. Please contact an administrator.",
        "es": "La IA está desactivada en esta plataforma, por lo que no puedo responder ahora. Póngase en contacto con un administrador.",
        "fr": "L'IA est désactivée sur cette plateforme ; je ne peux donc pas répondre pour le moment. Veuillez contacter un administrateur.",
        "ja": "このプラットフォームでは AI が無効なため、現在は応答できません。管理者にお問い合わせください。",
    },
    "ai_temporarily_unavailable": {
        "en": "The AI service is temporarily unavailable. Please try again in a few moments.",
        "es": "El servicio de IA no está disponible temporalmente. Vuelva a intentarlo en unos instantes.",
        "fr": "Le service d'IA est temporairement indisponible. Veuillez réessayer dans quelques instants.",
        "ja": "AI サービスは一時的に利用できません。しばらくしてからもう一度お試しください。",
    },
    "chat_unavailable_enterprise": {
        "en": "Chat is unavailable on this deployment. Enterprise Edition required. AI is not activated on this Community Edition deployment: connect an Enterprise Edition OpenCTI, OpenAEV or OpenCRQ platform, or install a Filigran license. Request a trial at https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ or contact sales at https://filigran.io/contact/",
        "es": "El chat no está disponible en esta implementación. Se requiere Enterprise Edition. La IA no está activada en esta implementación Community Edition: conecte una plataforma OpenCTI, OpenAEV u OpenCRQ Enterprise Edition o instale una licencia Filigran. Solicite una prueba en https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ o contacte con ventas en https://filigran.io/contact/",
        "fr": "Le chat est indisponible sur ce déploiement. Édition Enterprise requise. L'IA n'est pas activée sur ce déploiement Community Edition : connectez une plateforme OpenCTI, OpenAEV ou OpenCRQ en Édition Enterprise, ou installez une licence Filigran. Demandez un essai sur https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ ou contactez l'équipe commerciale sur https://filigran.io/contact/",
        "ja": "このデプロイメントではチャットを利用できません。Enterprise Edition が必要です。この Community Edition のデプロイメントでは AI が有効になっていません。Enterprise Edition の OpenCTI、OpenAEV、または OpenCRQ プラットフォームを接続するか、Filigran ライセンスをインストールしてください。トライアルは https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ からお申し込みいただくか、https://filigran.io/contact/ から営業担当にお問い合わせください。",
    },
    "chat_unavailable_license": {
        "en": "Chat is unavailable on this deployment. A valid Filigran license is required. AI is not licensed on this deployment: install a Filigran license (paste the PEM in Settings or set ENTERPRISE_LICENSE). Request a trial at https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ or contact sales at https://filigran.io/contact/",
        "es": "El chat no está disponible en esta implementación. Se requiere una licencia Filigran válida. La IA no tiene licencia en esta implementación: instale una licencia Filigran (pegue el PEM en Configuración o defina ENTERPRISE_LICENSE). Solicite una prueba en https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ o contacte con ventas en https://filigran.io/contact/",
        "fr": "Le chat est indisponible sur ce déploiement. Une licence Filigran valide est requise. L'IA n'est pas sous licence sur ce déploiement : installez une licence Filigran (collez le PEM dans Paramètres ou définissez ENTERPRISE_LICENSE). Demandez un essai sur https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ ou contactez l'équipe commerciale sur https://filigran.io/contact/",
        "ja": "このデプロイメントではチャットを利用できません。有効な Filigran ライセンスが必要です。このデプロイメントでは AI のライセンスがありません。Filigran ライセンスをインストールしてください（設定で PEM を貼り付けるか、ENTERPRISE_LICENSE を設定します）。トライアルは https://filigran.io/filigran-xtm-one-enterprise-edition-trial/ からお申し込みいただくか、https://filigran.io/contact/ から営業担当にお問い合わせください。",
    },
    # --- A conversation the web also reaches (services/channel_continuation) ---
    "channel_queued": {
        "en": "Got it. I'm still answering an earlier message in this conversation, and I'll answer this one right after.",
        "es": "Entendido. Todavía estoy respondiendo a un mensaje anterior de esta conversación y responderé a este justo después.",
        "fr": "Bien reçu. Je réponds encore à un message précédent de cette conversation, et je répondrai à celui-ci juste après.",
        "ja": "承知しました。この会話の前のメッセージにまだ回答中です。このメッセージにはその直後に回答します。",
    },
    "channel_queue_full": {
        "en": "I'm still answering earlier messages in this conversation and can't hold any more. Send this one again once those replies arrive.",
        "es": "Todavía estoy respondiendo a mensajes anteriores de esta conversación y no puedo retener más. Vuelva a enviar este cuando lleguen esas respuestas.",
        "fr": "Je réponds encore à des messages précédents de cette conversation et ne peux pas en mettre davantage en attente. Renvoyez celui-ci une fois ces réponses arrivées.",
        "ja": "この会話の前のメッセージにまだ回答中のため、これ以上は保留できません。それらの回答が届いてから、このメッセージをもう一度送信してください。",
    },
    "channel_queued_elsewhere": {
        "en": "This message was sent while this thread continued another conversation, which it no longer does, so it was not answered. Send it again to continue here.",
        "es": "Este mensaje se envió mientras este hilo continuaba otra conversación, que ya no continúa, por lo que no se respondió. Vuelva a enviarlo para continuar aquí.",
        "fr": "Ce message a été envoyé pendant que ce fil poursuivait une autre conversation, ce qui n'est plus le cas ; il n'a donc pas reçu de réponse. Renvoyez-le pour continuer ici.",
        "ja": "このメッセージは、このスレッドが別の会話を続けていた間に送信されました。現在はその会話を続けていないため、回答されませんでした。ここで続けるには、もう一度送信してください。",
    },
    "channel_approval_retired": {
        "en": "A tool call was waiting for your approval in the web app. This message moved the conversation on, so it was dropped.",
        "es": "Una llamada a una herramienta esperaba su aprobación en la aplicación web. Este mensaje hizo avanzar la conversación, por lo que se descartó.",
        "fr": "Un appel d'outil attendait votre approbation dans l'application web. Ce message a fait avancer la conversation : il a donc été abandonné.",
        "ja": "ツール呼び出しが Web アプリであなたの承認を待っていました。このメッセージで会話が先に進んだため、その呼び出しは破棄されました。",
    },
    "channel_turn_busy": {
        "en": "I'm still working on a previous message in this conversation. Send yours again once that reply arrives.",
        "es": "Todavía estoy trabajando en un mensaje anterior de esta conversación. Vuelva a enviar el suyo cuando llegue esa respuesta.",
        "fr": "Je traite encore un message précédent de cette conversation. Renvoyez le vôtre une fois cette réponse arrivée.",
        "ja": "この会話の前のメッセージをまだ処理中です。その回答が届いてから、もう一度送信してください。",
    },
    "channel_queued_dropped": {
        "en": "I couldn't answer a message you sent while I was answering an earlier one. Send it again if you still need an answer.",
        "es": "No pude responder a un mensaje que envió mientras respondía a uno anterior. Vuelva a enviarlo si todavía necesita una respuesta.",
        "fr": "Je n'ai pas pu répondre à un message que vous avez envoyé pendant que je répondais à un message précédent. Renvoyez-le si vous avez toujours besoin d'une réponse.",
        "ja": "前のメッセージに回答している間に送信されたメッセージに回答できませんでした。まだ回答が必要な場合は、もう一度送信してください。",
    },
    # A channel turn cut by a platform update (a rolling deploy, a restart)
    # that cannot be resumed: the sender sends it again. A resumed turn posts
    # nothing (#10772).
    "channel_turn_cut": {
        "en": "I was interrupted by a platform update before I could answer. Please send your message again.",
        "es": "Una actualización de la plataforma me interrumpió antes de poder responder. Vuelva a enviar su mensaje.",
        "fr": "J'ai été interrompu par une mise à jour de la plateforme avant de pouvoir répondre. Renvoyez votre message, s'il vous plaît.",
        "ja": "プラットフォームの更新により、回答する前に中断されました。もう一度メッセージを送信してください。",
    },
    "continuation_opening": {
        "en": "**{title}** continues here from {product}. {where} The full conversation stays in {product}: {link}",
        "es": "**{title}** continúa aquí desde {product}. {where} La conversación completa permanece en {product}: {link}",
        "fr": "**{title}** se poursuit ici depuis {product}. {where} La conversation complète reste dans {product} : {link}",
        "ja": "**{title}** の続きを {product} からここで行います。{where} 会話の全体は {product} に残ります: {link}",
    },
    "continuation_where_slack": {
        "en": "Reply in this thread to continue it here.",
        "es": "Responda en este hilo para continuarla aquí.",
        "fr": "Répondez dans ce fil pour la poursuivre ici.",
        "ja": "このスレッドで返信すると、ここで会話を続けられます。",
    },
    "continuation_where_teams": {
        "en": "Every message you send in this chat continues it, until you send /new.",
        "es": "Cada mensaje que envíe en este chat la continúa, hasta que envíe /new.",
        "fr": "Chaque message que vous envoyez dans ce chat la poursuit, jusqu'à ce que vous envoyiez /new.",
        "ja": "/new を送信するまで、このチャットで送信するすべてのメッセージで会話が続きます。",
    },
    # --- The channel adapters -----------------------------------------------
    "adapter_error": {
        "en": "Sorry, I encountered an error while processing your message. Please try again.",
        "es": "Lo siento, se produjo un error al procesar su mensaje. Vuelva a intentarlo.",
        "fr": "Désolé, une erreur s'est produite lors du traitement de votre message. Veuillez réessayer.",
        "ja": "申し訳ありません。メッセージの処理中にエラーが発生しました。もう一度お試しください。",
    },
    "teams_monitoring_stopped": {
        "en": "I've stopped monitoring this thread due to inactivity. Mention me again if you need my help!",
        "es": "He dejado de seguir este hilo por inactividad. Vuelva a mencionarme si necesita mi ayuda.",
        "fr": "J'ai cessé de suivre ce fil faute d'activité. Mentionnez-moi à nouveau si vous avez besoin de mon aide !",
        "ja": "操作がないため、このスレッドの監視を停止しました。サポートが必要なときは、もう一度メンションしてください。",
    },
    "google_chat_added": {
        "en": "Hello! I'm ready to help.",
        "es": "¡Hola! Estoy listo para ayudar.",
        "fr": "Bonjour ! Je suis prêt à vous aider.",
        "ja": "こんにちは！お手伝いする準備ができています。",
    },
    "google_chat_not_configured": {
        "en": "Bot not configured. Please contact your admin.",
        "es": "El bot no está configurado. Póngase en contacto con su administrador.",
        "fr": "Bot non configuré. Veuillez contacter votre administrateur.",
        "ja": "ボットが設定されていません。管理者にお問い合わせください。",
    },
    "discord_processing": {
        "en": "Processing your request...",
        "es": "Procesando su solicitud...",
        "fr": "Traitement de votre demande...",
        "ja": "リクエストを処理しています...",
    },
    "generated_image": {
        "en": "Generated image",
        "es": "Imagen generada",
        "fr": "Image générée",
        "ja": "生成された画像",
    },
    # The text of a link a channel reply gets for an agent's file, deck or
    # report whose name or title is empty (``channels/base.py``).
    "link_file": {
        "en": "file",
        "es": "archivo",
        "fr": "fichier",
        "ja": "ファイル",
    },
    "link_presentation": {
        "en": "presentation",
        "es": "presentación",
        "fr": "présentation",
        "ja": "プレゼンテーション",
    },
    "link_report": {
        "en": "report",
        "es": "informe",
        "fr": "rapport",
        "ja": "レポート",
    },
    "link_dashboard": {
        "en": "dashboard",
        "es": "panel",
        "fr": "tableau de bord",
        "ja": "ダッシュボード",
    },
    "link_video": {
        "en": "video",
        "es": "vídeo",
        "fr": "vidéo",
        "ja": "動画",
    },
    # --- @mentioned agents (services/mention_dispatch) ----------------------
    "mention_over_limit": {
        "en": "{name} did not answer: at most {limit} mentioned agents answer one message.",
        "es": "{name} no respondió: como máximo {limit} agentes mencionados responden a un mensaje.",
        "fr": "{name} n'a pas répondu : au plus {limit} agents mentionnés répondent à un même message.",
        "ja": "{name} は応答しませんでした: 1 件のメッセージに応答するメンションされたエージェントは最大 {limit} 件です。",
    },
    "mention_timeout": {
        "en": "{name} did not answer within the time this turn allows its mentioned agents.",
        "es": "{name} no respondió dentro del tiempo que este turno concede a sus agentes mencionados.",
        "fr": "{name} n'a pas répondu dans le temps que ce tour accorde à ses agents mentionnés.",
        "ja": "{name} は、このターンがメンションされたエージェントに与える時間内に応答しませんでした。",
    },
    "mention_not_asked": {
        "en": "Not asked: {names}.",
        "es": "Sin consultar: {names}.",
        "fr": "Non sollicités : {names}.",
        "ja": "依頼していないエージェント: {names}。",
    },
    "mention_failed": {
        "en": "{name} could not answer: {reason}",
        "es": "{name} no pudo responder: {reason}",
        "fr": "{name} n'a pas pu répondre : {reason}",
        "ja": "{name} は応答できませんでした: {reason}",
    },
    "mention_error_reason": {
        "en": "it hit an error while answering",
        "es": "se produjo un error al responder",
        "fr": "une erreur s'est produite pendant sa réponse",
        "ja": "応答中にエラーが発生しました",
    },
    "mention_empty": {
        "en": "{name} did not produce an answer.",
        "es": "{name} no generó ninguna respuesta.",
        "fr": "{name} n'a produit aucune réponse.",
        "ja": "{name} は回答を生成しませんでした。",
    },
    "mention_agent": {
        "en": "Agent",
        "es": "Agente",
        "fr": "Agent",
        "ja": "エージェント",
    },
    # --- A background task's completion post (services/sub_agent) ------------
    "task_completed": {
        "en": "**Background task completed** \u2014 {agent_name}",
        "es": "**Tarea en segundo plano completada** \u2014 {agent_name}",
        "fr": "**Tâche en arrière-plan terminée** \u2014 {agent_name}",
        "ja": "**バックグラウンドタスクが完了しました** \u2014 {agent_name}",
    },
    "task_no_output": {
        "en": "(No output.)",
        "es": "(Sin resultado.)",
        "fr": "(Aucun résultat.)",
        "ja": "（出力なし）",
    },
    # --- Written in English by a shared service, translated on the way out ---
    "quota_exceeded": {
        "en": "You have reached your agentic execution quota for the current period. Please contact your administrator or wait for the quota to reset.",
        "es": "Ha alcanzado su cuota de ejecuciones agénticas del periodo actual. Póngase en contacto con su administrador o espere a que se restablezca la cuota.",
        "fr": "Vous avez atteint votre quota d'exécutions agentiques pour la période en cours. Veuillez contacter votre administrateur ou attendre la réinitialisation du quota.",
        "ja": "現在の期間のエージェント実行クォータに達しました。管理者に問い合わせるか、クォータがリセットされるまでお待ちください。",
    },
    "quota_unavailable": {
        "en": "Unable to verify your usage quota because the quota backend is currently unavailable. Please try again later or contact your administrator.",
        "es": "No se puede verificar su cuota de uso porque el servicio de cuotas no está disponible en este momento. Vuelva a intentarlo más tarde o póngase en contacto con su administrador.",
        "fr": "Impossible de vérifier votre quota d'utilisation : le service des quotas est actuellement indisponible. Veuillez réessayer plus tard ou contacter votre administrateur.",
        "ja": "クォータのバックエンドが現在利用できないため、使用量のクォータを確認できません。後でもう一度お試しいただくか、管理者にお問い合わせください。",
    },
    "accountless_quota_exceeded": {
        "en": "This service is at capacity right now. Please try again later.",
        "es": "Este servicio está al máximo de su capacidad en este momento. Vuelva a intentarlo más tarde.",
        "fr": "Ce service a atteint sa capacité maximale pour le moment. Veuillez réessayer plus tard.",
        "ja": "このサービスは現在、処理能力の上限に達しています。後でもう一度お試しください。",
    },
    "accountless_quota_unavailable": {
        "en": "This service is temporarily unavailable. Please try again later.",
        "es": "Este servicio no está disponible temporalmente. Vuelva a intentarlo más tarde.",
        "fr": "Ce service est temporairement indisponible. Veuillez réessayer plus tard.",
        "ja": "このサービスは一時的に利用できません。後でもう一度お試しください。",
    },
    "guard_refused": {
        "en": "Sorry, I can't process that message \u2014 it was flagged by a safety rule. Please rephrase your request.",
        "es": "Lo siento, no puedo procesar ese mensaje: una regla de seguridad lo ha marcado. Reformule su solicitud.",
        "fr": "Désolé, je ne peux pas traiter ce message : une règle de sécurité l'a signalé. Veuillez reformuler votre demande.",
        "ja": "申し訳ありません。このメッセージは安全ルールによって検出されたため処理できません。リクエストを言い換えてください。",
    },
    "output_withheld": {
        "en": "The answer was withheld: a safety rule matched its content. Please rephrase your request or contact an administrator.",
        "es": "La respuesta se ha retenido: una regla de seguridad coincidió con su contenido. Reformule su solicitud o póngase en contacto con un administrador.",
        "fr": "La réponse a été retenue : une règle de sécurité correspondait à son contenu. Veuillez reformuler votre demande ou contacter un administrateur.",
        "ja": "回答は保留されました。安全ルールがその内容に一致しました。リクエストを言い換えるか、管理者にお問い合わせください。",
    },
    "approval_needed": {
        "en": "I need approval before running: {names}.\n\nThis chat can't collect that approval, so I've stopped rather than acting without it. You can allow this tool for yourself from the Tools page, or an administrator can allow it for everyone from the same place \u2014 or you can ask me to do something else.",
        "es": "Necesito aprobación antes de ejecutar: {names}.\n\nEste chat no puede recoger esa aprobación, así que me he detenido en lugar de actuar sin ella. Puede permitir esta herramienta para usted desde la página Herramientas, o un administrador puede permitirla para todos desde el mismo lugar; también puede pedirme otra cosa.",
        "fr": "J'ai besoin d'une approbation avant d'exécuter : {names}.\n\nCe chat ne peut pas recueillir cette approbation ; je me suis donc arrêté plutôt que d'agir sans elle. Vous pouvez autoriser cet outil pour vous-même depuis la page Outils, ou un administrateur peut l'autoriser pour tout le monde au même endroit. Vous pouvez aussi me demander autre chose.",
        "ja": "次を実行する前に承認が必要です: {names}。\n\nこのチャットではその承認を受け付けられないため、承認なしで実行せずに停止しました。ツール ページで自分用にこのツールを許可するか、管理者が同じ場所から全員に対して許可できます。または、別の作業を依頼してください。",
    },
    "approval_needed_here": {
        "en": "I need approval before running: {names}.\n\nI can't collect that approval here, so I've stopped rather than acting without it. Please ask me to do something else.",
        "es": "Necesito aprobación antes de ejecutar: {names}.\n\nNo puedo recoger esa aprobación aquí, así que me he detenido en lugar de actuar sin ella. Pídame otra cosa.",
        "fr": "J'ai besoin d'une approbation avant d'exécuter : {names}.\n\nJe ne peux pas recueillir cette approbation ici ; je me suis donc arrêté plutôt que d'agir sans elle. Veuillez me demander autre chose.",
        "ja": "次を実行する前に承認が必要です: {names}。\n\nここではその承認を受け付けられないため、承認なしで実行せずに停止しました。別の作業を依頼してください。",
    },
    "approval_abandoned": {
        "en": "This turn needed approval before it could run a tool, and nobody was here to give it, so it was stopped. Ask again when you are ready to answer.",
        "es": "Este turno necesitaba aprobación antes de ejecutar una herramienta y nadie estaba disponible para darla, así que se detuvo. Vuelva a pedirlo cuando esté listo para responder.",
        "fr": "Ce tour avait besoin d'une approbation avant d'exécuter un outil, et personne n'était là pour la donner : il a donc été arrêté. Redemandez quand vous serez prêt à répondre.",
        "ja": "このターンではツールを実行する前に承認が必要でしたが、承認する人がいなかったため停止しました。応答できるようになったら、もう一度依頼してください。",
    },
    "turn_timeout": {
        "en": "I'm sorry, this request took too long and was stopped. Please try a simpler question or try again later.",
        "es": "Lo siento, esta solicitud tardó demasiado y se detuvo. Pruebe con una pregunta más sencilla o vuelva a intentarlo más tarde.",
        "fr": "Désolé, cette demande a pris trop de temps et a été arrêtée. Essayez une question plus simple ou réessayez plus tard.",
        "ja": "申し訳ありません。このリクエストは時間がかかりすぎたため停止しました。より簡単な質問にするか、後でもう一度お試しください。",
    },
    "model_timeout": {
        "en": "I'm sorry, the AI model took too long to respond. Please try again.",
        "es": "Lo siento, el modelo de IA tardó demasiado en responder. Vuelva a intentarlo.",
        "fr": "Désolé, le modèle d'IA a mis trop de temps à répondre. Veuillez réessayer.",
        "ja": "申し訳ありません。AI モデルの応答に時間がかかりすぎました。もう一度お試しください。",
    },
    "context_too_many_tools": {
        "en": "This agent has too many tools configured for the AI model's context window. An administrator may need to reduce the number of bound tools or switch to a model with a larger context.",
        "es": "Este agente tiene demasiadas herramientas configuradas para la ventana de contexto del modelo de IA. Es posible que un administrador deba reducir el número de herramientas vinculadas o cambiar a un modelo con un contexto mayor.",
        "fr": "Cet agent a trop d'outils configurés pour la fenêtre de contexte du modèle d'IA. Un administrateur devra peut-être réduire le nombre d'outils liés ou passer à un modèle disposant d'un contexte plus large.",
        "ja": "このエージェントには、AI モデルのコンテキストウィンドウに対して多すぎるツールが設定されています。管理者が紐付けたツールの数を減らすか、より大きなコンテキストを持つモデルに切り替える必要があるかもしれません。",
    },
    "context_too_long": {
        "en": "I'm sorry, the conversation has grown too large for the AI model to process. Please start a new conversation or ask a shorter question.",
        "es": "Lo siento, la conversación ha crecido demasiado para que el modelo de IA la procese. Inicie una conversación nueva o haga una pregunta más breve.",
        "fr": "Désolé, la conversation est devenue trop longue pour que le modèle d'IA puisse la traiter. Veuillez démarrer une nouvelle conversation ou poser une question plus courte.",
        "ja": "申し訳ありません。会話が大きくなりすぎて AI モデルで処理できません。新しい会話を始めるか、より短い質問をしてください。",
    },
    "request_too_large": {
        "en": "I'm sorry, the request is too large for the AI model to process. Your conversation may have grown too long \u2014 please start a new conversation. If the issue persists with a fresh conversation, try reducing the size of any attachments.",
        "es": "Lo siento, la solicitud es demasiado grande para que el modelo de IA la procese. Es posible que la conversación se haya alargado demasiado: inicie una conversación nueva. Si el problema persiste con una conversación nueva, intente reducir el tamaño de los archivos adjuntos.",
        "fr": "Désolé, la demande est trop volumineuse pour être traitée par le modèle d'IA. Votre conversation est peut-être devenue trop longue : veuillez démarrer une nouvelle conversation. Si le problème persiste avec une nouvelle conversation, essayez de réduire la taille des pièces jointes.",
        "ja": "申し訳ありません。リクエストが大きすぎて AI モデルで処理できません。会話が長くなりすぎた可能性があります。新しい会話を始めてください。新しい会話でも問題が続く場合は、添付ファイルのサイズを小さくしてみてください。",
    },
    "request_cancelled": {
        "en": "The request was cancelled.",
        "es": "La solicitud se canceló.",
        "fr": "La demande a été annulée.",
        "ja": "リクエストはキャンセルされました。",
    },
    "empty_response": {
        "en": "I gathered information but was unable to compose a complete response. Please try rephrasing your question or narrowing the scope.",
        "es": "Reuní información, pero no pude redactar una respuesta completa. Intente reformular la pregunta o acotar su alcance.",
        "fr": "J'ai recueilli des informations, mais je n'ai pas pu rédiger une réponse complète. Essayez de reformuler votre question ou d'en restreindre la portée.",
        "ja": "情報は収集しましたが、完全な回答を作成できませんでした。質問を言い換えるか、範囲を絞ってみてください。",
    },
    "forced_answer_timeout": {
        "en": "The agent ran out of time while composing the final answer after using its tools.",
        "es": "Al agente se le acabó el tiempo mientras redactaba la respuesta final tras usar sus herramientas.",
        "fr": "L'agent a manqué de temps en rédigeant la réponse finale après avoir utilisé ses outils.",
        "ja": "エージェントはツールを使用した後、最終回答の作成中に時間切れになりました。",
    },
    "forced_answer_error": {
        "en": "The agent hit an internal error while composing the final answer after using its tools.",
        "es": "El agente sufrió un error interno mientras redactaba la respuesta final tras usar sus herramientas.",
        "fr": "L'agent a rencontré une erreur interne en rédigeant la réponse finale après avoir utilisé ses outils.",
        "ja": "エージェントはツールを使用した後、最終回答の作成中に内部エラーが発生しました。",
    },
    "partial_results": {
        "en": "{lead}\n\nPartial results:\n\n{partial}",
        "es": "{lead}\n\nResultados parciales:\n\n{partial}",
        "fr": "{lead}\n\nRésultats partiels :\n\n{partial}",
        "ja": "{lead}\n\n途中の結果:\n\n{partial}",
    },
    # The per-run budget (``tools/agent_loop.py``), with the
    # exhausted dimension (``run_budget.BudgetViolation.user_message``).
    "budget_exhausted": {
        "en": "The agent stopped because {reason}, leaving too little budget to compose a final answer.",
        "es": "El agente se detuvo porque {reason}, lo que dejó muy poco presupuesto para redactar una respuesta final.",
        "fr": "L'agent s'est arrêté parce que {reason}, ce qui laisse trop peu de budget pour rédiger une réponse finale.",
        "ja": "{reason}ため、エージェントは停止しました。最終回答を作成するための予算がほとんど残っていません。",
    },
    "model_downgrade": {
        "en": "\u26a0\ufe0f Continuing on a cheaper model ({to_model}): {reason} on the primary model. The answer may be less detailed than usual.",
        "es": "\u26a0\ufe0f Se continúa con un modelo más económico ({to_model}): {reason} en el modelo principal. La respuesta puede ser menos detallada de lo habitual.",
        "fr": "\u26a0\ufe0f Poursuite sur un modèle moins coûteux ({to_model}) : {reason} sur le modèle principal. La réponse peut être moins détaillée que d'habitude.",
        "ja": "\u26a0\ufe0f より安価なモデル（{to_model}）で続行します: メインモデルで{reason}ためです。回答はいつもより簡潔になる場合があります。",
    },
    "budget_reason_cost": {
        "en": "this run reached its cost budget (${amount})",
        "es": "esta ejecución alcanzó su presupuesto de coste (${amount})",
        "fr": "cette exécution a atteint son budget de coût (${amount})",
        "ja": "この実行がコスト予算（${amount}）に達した",
    },
    "budget_reason_tokens": {
        "en": "this run reached its token budget ({count} tokens)",
        "es": "esta ejecución alcanzó su presupuesto de tokens ({count} tokens)",
        "fr": "cette exécution a atteint son budget de jetons ({count} jetons)",
        "ja": "この実行がトークン予算（{count} トークン）に達した",
    },
    "budget_reason_tool_calls": {
        "en": "this run reached its tool-call budget ({count} calls)",
        "es": "esta ejecución alcanzó su presupuesto de llamadas a herramientas ({count} llamadas)",
        "fr": "cette exécution a atteint son budget d'appels d'outils ({count} appels)",
        "ja": "この実行がツール呼び出し予算（{count} 回）に達した",
    },
    "budget_reason_time": {
        "en": "this run reached its time budget ({seconds}s)",
        "es": "esta ejecución alcanzó su presupuesto de tiempo ({seconds} s)",
        "fr": "cette exécution a atteint son budget de temps ({seconds} s)",
        "ja": "この実行が時間予算（{seconds} 秒）に達した",
    },
    "assignment_failed": {
        "en": "Assignment '{name}' failed.",
        "es": "La asignación '{name}' ha fallado.",
        "fr": "L'assignation « {name} » a échoué.",
        "ja": "アサインメント「{name}」が失敗しました。",
    },
    "assignment_completed": {
        "en": "Assignment '{name}' completed.",
        "es": "La asignación '{name}' se ha completado.",
        "fr": "L'assignation « {name} » est terminée.",
        "ja": "アサインメント「{name}」が完了しました。",
    },
    # --- The card of a pasted XTM One link ------------------------
    "card_header_run": {
        "en": "XTM One · Assignment run",
        "es": "XTM One · Ejecución de asignación",
        "fr": "XTM One · Exécution d'assignation",
        "ja": "XTM One · アサインメントの実行",
    },
    "card_neutral_hint": {
        "en": "Details load for each viewer. If they don't, choose Refresh card in the message menu.",
        "es": "Los detalles se cargan para cada persona que ve la tarjeta. Si no aparecen, elija Actualizar tarjeta en el menú del mensaje.",
        "fr": "Les détails se chargent pour chaque personne qui consulte la carte. S'ils n'apparaissent pas, choisissez Actualiser la carte dans le menu du message.",
        "ja": "詳細は閲覧者ごとに読み込まれます。表示されない場合は、メッセージのメニューから［カードを更新］を選択してください。",
    },
    "card_open": {
        "en": "Open in XTM One",
        "es": "Abrir en XTM One",
        "fr": "Ouvrir dans XTM One",
        "ja": "XTM One で開く",
    },
    # --- The link buttons of the objects of a decision's ask (#1177) ------
    "card_link_pull_request": {
        "en": "Open PR #{number}",
        "es": "Abrir la PR #{number}",
        "fr": "Ouvrir la PR #{number}",
        "ja": "PR #{number} を開く",
    },
    "card_link_issue": {
        "en": "Open issue #{number}",
        "es": "Abrir la incidencia #{number}",
        "fr": "Ouvrir l'issue #{number}",
        "ja": "Issue #{number} を開く",
    },
    "card_link_ticket": {
        "en": "Open the ticket",
        "es": "Abrir el ticket",
        "fr": "Ouvrir le ticket",
        "ja": "チケットを開く",
    },
    "card_link_ticket_key": {
        "en": "Open {key}",
        "es": "Abrir {key}",
        "fr": "Ouvrir {key}",
        "ja": "{key} を開く",
    },
    "card_link_page": {
        "en": "Open the page",
        "es": "Abrir la página",
        "fr": "Ouvrir la page",
        "ja": "ページを開く",
    },
    "card_link_document": {
        "en": "Open the document",
        "es": "Abrir el documento",
        "fr": "Ouvrir le document",
        "ja": "ドキュメントを開く",
    },
    "card_no_access": {
        "en": "This run doesn't exist or you don't have access to it.",
        "es": "Esta ejecución no existe o no tiene acceso a ella.",
        "fr": "Cette exécution n'existe pas ou vous n'y avez pas accès.",
        "ja": "この実行は存在しないか、アクセス権がありません。",
    },
    "card_not_linked": {
        "en": "Sign in to XTM One with the email address of this account to see this run.",
        "es": "Inicie sesión en XTM One con la dirección de correo de esta cuenta para ver esta ejecución.",
        "fr": "Connectez-vous à XTM One avec l'adresse e-mail de ce compte pour voir cette exécution.",
        "ja": "この実行を表示するには、このアカウントのメールアドレスで XTM One にサインインしてください。",
    },
    "card_label_agent": {
        "en": "Agent",
        "es": "Agente",
        "fr": "Agent",
        "ja": "エージェント",
    },
    "card_label_assignment": {
        "en": "Assignment",
        "es": "Asignación",
        "fr": "Assignation",
        "ja": "アサインメント",
    },
    "card_label_status": {
        "en": "Status",
        "es": "Estado",
        "fr": "Statut",
        "ja": "ステータス",
    },
    "card_label_started": {
        "en": "Started",
        "es": "Iniciada",
        "fr": "Démarrée",
        "ja": "開始",
    },
    "card_status_pending": {
        "en": "Pending",
        "es": "Pendiente",
        "fr": "En attente",
        "ja": "保留中",
    },
    "card_status_delayed": {
        "en": "Delayed",
        "es": "Aplazada",
        "fr": "Différée",
        "ja": "延期",
    },
    "card_status_running": {
        "en": "Running",
        "es": "En curso",
        "fr": "En cours",
        "ja": "実行中",
    },
    "card_status_awaiting_human": {
        "en": "Waiting for a person",
        "es": "Esperando a una persona",
        "fr": "En attente d'une personne",
        "ja": "人の対応待ち",
    },
    "card_status_completed": {
        "en": "Completed",
        "es": "Completada",
        "fr": "Terminée",
        "ja": "完了",
    },
    "card_status_failed": {
        "en": "Failed",
        "es": "Fallida",
        "fr": "Échouée",
        "ja": "失敗",
    },
    "card_status_skipped": {
        "en": "Skipped",
        "es": "Omitida",
        "fr": "Ignorée",
        "ja": "スキップ",
    },
    "card_tools_awaiting": {
        "en": "Tools awaiting approval:",
        "es": "Herramientas pendientes de aprobación:",
        "fr": "Outils en attente d'approbation :",
        "ja": "承認待ちのツール：",
    },
    "card_question": {
        "en": "The agent asks:",
        "es": "El agente pregunta:",
        "fr": "L'agent demande :",
        "ja": "エージェントからの質問：",
    },
    "card_approve": {
        "en": "Approve",
        "es": "Aprobar",
        "fr": "Approuver",
        "ja": "承認",
    },
    "card_approve_always": {
        "en": "Approve always",
        "es": "Aprobar siempre",
        "fr": "Toujours approuver",
        "ja": "常に承認",
    },
    "card_reject": {
        "en": "Reject",
        "es": "Rechazar",
        "fr": "Rejeter",
        "ja": "却下",
    },
    "card_approve_all": {
        "en": "Approve all",
        "es": "Aprobar todo",
        "fr": "Tout approuver",
        "ja": "すべて承認",
    },
    "card_reject_all": {
        "en": "Reject all",
        "es": "Rechazar todo",
        "fr": "Tout rejeter",
        "ja": "すべて却下",
    },
    "card_submit": {
        "en": "Submit",
        "es": "Enviar",
        "fr": "Valider",
        "ja": "送信",
    },
    "card_reason": {
        "en": "Reason (optional)",
        "es": "Motivo (opcional)",
        "fr": "Motif (facultatif)",
        "ja": "理由（任意）",
    },
    "card_answer": {
        "en": "Your answer",
        "es": "Su respuesta",
        "fr": "Votre réponse",
        "ja": "回答",
    },
    "card_send": {
        "en": "Send",
        "es": "Enviar",
        "fr": "Envoyer",
        "ja": "送信",
    },
    "card_too_many_tools": {
        "en": "{count} tools await approval: decide them in XTM One.",
        "es": "{count} herramientas esperan aprobación: decida en XTM One.",
        "fr": "{count} outils attendent une approbation : décidez-en dans XTM One.",
        "ja": "{count} 個のツールが承認待ちです。XTM One で判断してください。",
    },
    "card_waiting_admin": {
        "en": "Waiting for an administrator to answer.",
        "es": "Esperando la respuesta de un administrador.",
        "fr": "En attente de la réponse d'un administrateur.",
        "ja": "管理者の回答を待っています。",
    },
    "card_answered_by": {
        "en": "Answered by {name}.",
        "es": "Respondida por {name}.",
        "fr": "Réponse donnée par {name}.",
        "ja": "{name} が回答しました。",
    },
    "card_refused": {
        "en": "You can't respond to this run.",
        "es": "No puede responder a esta ejecución.",
        "fr": "Vous ne pouvez pas répondre à cette exécution.",
        "ja": "この実行に回答することはできません。",
    },
    "card_already_answered": {
        "en": "This run has already been answered.",
        "es": "Esta ejecución ya ha sido respondida.",
        "fr": "Cette exécution a déjà reçu une réponse.",
        "ja": "この実行にはすでに回答済みです。",
    },
    "card_changed": {
        "en": "This request changed. Check it again.",
        "es": "Esta solicitud ha cambiado. Revísela de nuevo.",
        "fr": "Cette demande a changé. Vérifiez-la à nouveau.",
        "ja": "このリクエストは変更されました。もう一度確認してください。",
    },
    "card_decide_every": {
        "en": "Choose Approve or Reject for every tool.",
        "es": "Elija Aprobar o Rechazar para cada herramienta.",
        "fr": "Choisissez Approuver ou Rejeter pour chaque outil.",
        "ja": "すべてのツールについて［承認］または［却下］を選択してください。",
    },
    "card_choose_approval": {
        "en": "Choose Approve, Approve always or Reject.",
        "es": "Elija Aprobar, Aprobar siempre o Rechazar.",
        "fr": "Choisissez Approuver, Toujours approuver ou Rejeter.",
        "ja": "［承認］、［常に承認］、［却下］のいずれかを選択してください。",
    },
    "card_answer_empty": {
        "en": "Write an answer first.",
        "es": "Escriba primero una respuesta.",
        "fr": "Écrivez d'abord une réponse.",
        "ja": "先に回答を入力してください。",
    },
    "card_failed": {
        "en": "This couldn't be done. Open the run in XTM One.",
        "es": "No se pudo realizar. Abra la ejecución en XTM One.",
        "fr": "Impossible de le faire. Ouvrez l'exécution dans XTM One.",
        "ja": "実行できませんでした。XTM One で実行を開いてください。",
    },
    "card_ask_owner": {
        "en": "Ask the owner to act",
        "es": "Pedir al propietario que actúe",
        "fr": "Demander au propriétaire d'agir",
        "ja": "所有者に対応を依頼",
    },
    "card_ask_admin": {
        "en": "Ask an admin to act",
        "es": "Pedir a un administrador que actúe",
        "fr": "Demander à un administrateur d'agir",
        "ja": "管理者に対応を依頼",
    },
    "card_request_sent": {
        "en": "Your request has been sent.",
        "es": "Su solicitud se ha enviado.",
        "fr": "Votre demande a été envoyée.",
        "ja": "依頼を送信しました。",
    },
    "card_answer_sent": {
        "en": "Your answer was sent.",
        "es": "Su respuesta se ha enviado.",
        "fr": "Votre réponse a été envoyée.",
        "ja": "回答を送信しました。",
    },
    # --- The decision card of any human gate (#842, S9) -------------
    "card_header_decision": {
        "en": "XTM One · Decision",
        "es": "XTM One · Decisión",
        "fr": "XTM One · Décision",
        "ja": "XTM One · 判断",
    },
    "card_from": {
        "en": "{source}",
        "es": "{source}",
        "fr": "{source}",
        "ja": "{source}",
    },
    "card_from_by": {
        "en": "{source} from {agent}",
        "es": "{source} de {agent}",
        "fr": "{source} de {agent}",
        "ja": "{agent} からの{source}",
    },
    "card_source_run": {
        "en": "Question",
        "es": "Pregunta",
        "fr": "Question",
        "ja": "質問",
    },
    "card_source_tool_approval": {
        "en": "Tool approval",
        "es": "Aprobación de herramienta",
        "fr": "Approbation d'outil",
        "ja": "ツールの承認",
    },
    "card_source_flow": {
        "en": "Flow question",
        "es": "Pregunta de un flujo",
        "fr": "Question d'un flux",
        "ja": "フローからの質問",
    },
    "card_source_work_item": {
        "en": "Work item decision",
        "es": "Decisión de una tarea",
        "fr": "Décision sur une tâche",
        "ja": "作業項目の判断",
    },
    "card_recommended": {
        "en": "Recommended",
        "es": "Recomendada",
        "fr": "Recommandée",
        "ja": "推奨",
    },
    "card_label_deadline": {
        "en": "Answer by",
        "es": "Responder antes de",
        "fr": "Répondre avant",
        "ja": "回答期限",
    },
    "card_label_answered_at": {
        "en": "Answered at",
        "es": "Respondida el",
        "fr": "Répondue le",
        "ja": "回答日時",
    },
    "card_decision_waiting": {
        "en": "A decision is waiting for you",
        "es": "Una decisión le espera",
        "fr": "Une décision vous attend",
        "ja": "判断が必要です",
    },
    "card_answer_text": {
        "en": "Add a comment or answer in your own words (optional)",
        "es": "Añada un comentario o responda con sus palabras (opcional)",
        "fr": "Ajoutez un commentaire ou répondez avec vos mots (facultatif)",
        "ja": "コメントや自由回答を入力（任意）",
    },
    "card_waiting_for": {
        "en": "Waiting for {name}.",
        "es": "Esperando a {name}.",
        "fr": "En attente de {name}.",
        "ja": "{name} の回答待ちです。",
    },
    "card_not_linked_decision": {
        "en": "Sign in to XTM One with the email address of this account to answer.",
        "es": "Inicie sesión en XTM One con la dirección de correo de esta cuenta para responder.",
        "fr": "Connectez-vous à XTM One avec l'adresse e-mail de ce compte pour répondre.",
        "ja": "回答するには、このアカウントのメールアドレスで XTM One にサインインしてください。",
    },
    "card_decided_option": {
        "en": "{name} chose {option}.",
        "es": "{name} eligió {option}.",
        "fr": "{name} a choisi {option}.",
        "ja": "{name} が {option} を選びました。",
    },
    "card_decided_tools": {
        "en": "{name} decided the tools: {verdicts}.",
        "es": "{name} decidió sobre las herramientas: {verdicts}.",
        "fr": "{name} a décidé pour les outils : {verdicts}.",
        "ja": "{name} がツールについて判断しました: {verdicts}。",
    },
    "card_decision_expired": {
        "en": "This decision expired without an answer.",
        "es": "Esta decisión venció sin respuesta.",
        "fr": "Cette décision a expiré sans réponse.",
        "ja": "この判断は回答されないまま期限切れになりました。",
    },
    "card_decision_cancelled": {
        "en": "This decision is no longer needed.",
        "es": "Esta decisión ya no es necesaria.",
        "fr": "Cette décision n'est plus nécessaire.",
        "ja": "この判断は不要になりました。",
    },
    "card_someone": {
        "en": "Someone",
        "es": "Alguien",
        "fr": "Quelqu'un",
        "ja": "誰か",
    },
    "card_recipient_company": {
        "en": "anyone in the company",
        "es": "cualquier persona de la empresa",
        "fr": "n'importe qui dans l'entreprise",
        "ja": "社内のいずれかのメンバー",
    },
    "card_recipient_admins": {
        "en": "the platform administrators",
        "es": "los administradores de la plataforma",
        "fr": "les administrateurs de la plateforme",
        "ja": "プラットフォームの管理者",
    },
    "card_decision_refused": {
        "en": "You can't answer this decision.",
        "es": "No puede responder a esta decisión.",
        "fr": "Vous ne pouvez pas répondre à cette décision.",
        "ja": "この判断に回答することはできません。",
    },
    "card_decision_already_answered": {
        "en": "This decision has already been answered.",
        "es": "Esta decisión ya ha sido respondida.",
        "fr": "Cette décision a déjà reçu une réponse.",
        "ja": "この判断にはすでに回答済みです。",
    },
    # --- The Slack decision card (#842, S10) ---------------------------
    "card_answer_with_text": {
        "en": "Answer with text",
        "es": "Responder con texto",
        "fr": "Répondre par écrit",
        "ja": "テキストで回答",
    },
    "card_modal_title": {
        "en": "Your answer",
        "es": "Su respuesta",
        "fr": "Votre réponse",
        "ja": "回答",
    },
    "card_cancel": {
        "en": "Cancel",
        "es": "Cancelar",
        "fr": "Annuler",
        "ja": "キャンセル",
    },
    "card_decision_failed": {
        "en": "This couldn't be done. Open the decision in XTM One.",
        "es": "No se pudo realizar. Abra la decisión en XTM One.",
        "fr": "L'action n'a pas abouti. Ouvrez la décision dans XTM One.",
        "ja": "実行できませんでした。XTM One で判断を開いてください。",
    },
    # --- The WhatsApp decision message (#842, S12) ----------------------
    "card_choose": {
        "en": "Choose",
        "es": "Elegir",
        "fr": "Choisir",
        "ja": "選択",
    },
    "card_decide_in_xtm": {
        "en": "Answer in XTM One.",
        "es": "Responda en XTM One.",
        "fr": "Répondez dans XTM One.",
        "ja": "XTM One で回答してください。",
    },
}

#: The answer of a run the budget stopped with nothing to show: the agent loop
#: writes the two sentences one after the other (``_budget_exhausted_text``).
NOTICES["budget_exhausted_no_answer"] = {
    language: NOTICES["budget_exhausted"][language]
    + ("" if language == "ja" else " ")
    + NOTICES["empty_response"][language]
    for language in LANGUAGES
}

#: The exhausted dimension a budget notice names: never a reply of its own,
#: translated inside the notice that carries it.
BUDGET_REASONS: tuple[str, ...] = (
    "budget_reason_cost",
    "budget_reason_tokens",
    "budget_reason_tool_calls",
    "budget_reason_time",
)

#: The notices a service shared with the web chat writes in English into what
#: it stores, recognised by :func:`localized_reply` - the most specific first.
REPLY_NOTICES: tuple[str, ...] = (
    "approval_needed",
    "approval_needed_here",
    "partial_results",
    "budget_exhausted_no_answer",
    "budget_exhausted",
    "model_downgrade",
    "quota_exceeded",
    "quota_unavailable",
    "accountless_quota_exceeded",
    "accountless_quota_unavailable",
    "guard_refused",
    "output_withheld",
    "approval_abandoned",
    "turn_timeout",
    "model_timeout",
    "context_too_many_tools",
    "context_too_long",
    "request_too_large",
    "request_cancelled",
    "empty_response",
    "forced_answer_timeout",
    "forced_answer_error",
    "assignment_failed",
    "assignment_completed",
)

#: A reply notice's placeholder that holds another notice, with the notices it
#: may hold: translated in turn, and the template only applies when it is one.
_NESTED_NOTICES: dict[str, tuple[str, tuple[str, ...]]] = {
    "partial_results": ("lead", REPLY_NOTICES),
    "budget_exhausted": ("reason", BUDGET_REASONS),
    "budget_exhausted_no_answer": ("reason", BUDGET_REASONS),
    "model_downgrade": ("reason", BUDGET_REASONS),
}

#: Reply notices the agent loop appends to an answer it could not finish
#: (``_say_budget_cut``): the answer, a blank line, the notice.
TRAILING_NOTICES: tuple[str, ...] = ("budget_exhausted",)


def catalog_language(value: object) -> str | None:
    """The catalog language of a stored language code (``fr-fr`` -> ``fr``).

    ``None`` for ``auto``, an empty value or a language the catalog does not hold.
    """
    code = normalize_language(value)
    if not code or code == LANGUAGE_AUTO:
        return None
    language = code.split("-", 1)[0]
    return language if language in LANGUAGES else None


def platform_language() -> str | None:
    """The platform default language, ``None`` when it is ``auto`` (unset)."""
    from app.services.branding import get_cached_branding

    return catalog_language(get_cached_branding().language)


def notice_language(user: Any = None) -> str:
    """The language a notice to *user* is written in.

    The user's own preference, else the platform default, else English. With
    no user (a sender the platform has not identified) the platform default.
    """
    return catalog_language(getattr(user, "language", None)) or platform_language() or DEFAULT_LANGUAGE


async def user_notice_language(db: Any, user_id: Any) -> str:
    """:func:`notice_language` for a user known by id only (``None``: no user)."""
    if user_id is None:
        return notice_language()
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except ValueError:
        return notice_language()
    from sqlalchemy import select

    from app.models.user import User

    language = await db.scalar(select(User.language).where(User.id == uid))
    return catalog_language(language) or platform_language() or DEFAULT_LANGUAGE


def placeholders(template: str) -> frozenset[str]:
    """The ``{name}`` placeholders of a catalog text."""
    return frozenset(field for _, field, _, _ in string.Formatter().parse(template) if field)


def notice(key: str, language: str | None = None, /, **params: object) -> str:
    """The notice *key* in *language*, English when it holds no translation."""
    texts = NOTICES[key]
    template = texts.get(language or DEFAULT_LANGUAGE) or texts[DEFAULT_LANGUAGE]
    return template.format(**params) if params else template


def chat_unavailable_notice(language: str | None = None) -> str:
    """``deps.chat_unavailable_detail()`` - the chat gate's licence refusal - in *language*."""
    from app.core.config import settings

    key = "chat_unavailable_license" if settings.is_copilot_like else "chat_unavailable_enterprise"
    return notice(key, language)


@functools.cache
def _reply_pattern(key: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[a-z_]+\})", NOTICES[key][DEFAULT_LANGUAGE])
    return re.compile(
        "".join(
            f"(?P<{part[1:-1]}>.+?)" if part.startswith("{") and part.endswith("}") else re.escape(part)
            for part in parts
        ),
        re.DOTALL,
    )


def _translated(text: str, language: str, keys: tuple[str, ...]) -> str | None:
    """*text* in *language* when it is one of *keys* as a whole, else ``None``."""
    for key in keys:
        match = _reply_pattern(key).fullmatch(text)
        if match is None:
            continue
        params = match.groupdict()
        nested = _NESTED_NOTICES.get(key)
        if nested is not None:
            name, candidates = nested
            inner = _translated(params[name], language, candidates)
            if inner is None:
                continue
            params[name] = inner
        return notice(key, language, **params)
    return None


def localized_reply(text: str, language: str | None) -> str:
    """*text* in *language* when it is one of :data:`REPLY_NOTICES`, or an
    answer one of :data:`TRAILING_NOTICES` ends; else as is."""
    if not text or not language or language == DEFAULT_LANGUAGE:
        return text
    whole = _translated(text, language, REPLY_NOTICES)
    if whole is not None:
        return whole
    head, blank, tail = text.rpartition("\n\n")
    if blank:
        trailing = _translated(tail, language, TRAILING_NOTICES)
        if trailing is not None:
            return f"{head}{blank}{trailing}"
    return text
