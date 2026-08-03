// Copyright soft-ue-expert. All Rights Reserved.

#include "Tools/SessionChannelTool.h"
#include "Session/BridgeSessionRegistry.h"

namespace
{
	FBridgeSchemaProperty MakeProperty(const TCHAR* Type, const TCHAR* Description)
	{
		FBridgeSchemaProperty Property;
		Property.Type = Type;
		Property.Description = Description;
		return Property;
	}

	/** The roster minus the caller: nobody needs to be told they are present. */
	TArray<TSharedPtr<FJsonValue>> RosterExcluding(const FString& ExcludeId, bool bIncludeStale)
	{
		const FDateTime NowUtc = FDateTime::UtcNow();
		TArray<TSharedPtr<FJsonValue>> Out;
		for (const FSessionRecord& Record : FBridgeSessionRegistry::Get().ListSessions(bIncludeStale))
		{
			if (Record.Id == ExcludeId)
			{
				continue;
			}
			Out.Add(MakeShared<FJsonValueObject>(FBridgeSessionRegistry::RecordToJson(Record, NowUtc)));
		}
		return Out;
	}

	/** Ids a message can still reach: everyone but the sender who has not left. */
	TArray<TSharedPtr<FJsonValue>> AddressableIds(const FString& ExcludeId)
	{
		TArray<TSharedPtr<FJsonValue>> Out;
		for (const FSessionRecord& Record : FBridgeSessionRegistry::Get().ListSessions(true))
		{
			if (Record.Id == ExcludeId || Record.bEnded)
			{
				continue;
			}
			Out.Add(MakeShared<FJsonValueString>(Record.Id));
		}
		return Out;
	}

	/** Match a requested target by id first, then by label. Empty when nobody matches. */
	FString ResolveTargetId(const FString& Requested)
	{
		if (Requested.IsEmpty())
		{
			return FString();
		}

		const TArray<FSessionRecord> Records = FBridgeSessionRegistry::Get().ListSessions(true);
		for (const FSessionRecord& Record : Records)
		{
			if (Record.Id == Requested)
			{
				return Record.Id;
			}
		}
		for (const FSessionRecord& Record : Records)
		{
			if (!Record.Label.IsEmpty() && Record.Label == Requested)
			{
				return Record.Id;
			}
		}
		return FString();
	}

	TArray<FString> ParseResources(const TSharedPtr<FJsonObject>& Args, bool& bOutPresent)
	{
		bOutPresent = false;
		TArray<FString> Resources;

		const TArray<TSharedPtr<FJsonValue>>* Values = nullptr;
		if (!Args.IsValid() || !Args->TryGetArrayField(TEXT("resources"), Values) || !Values)
		{
			return Resources;
		}

		bOutPresent = true;
		for (const TSharedPtr<FJsonValue>& Value : *Values)
		{
			FString Resource;
			if (Value.IsValid() && Value->TryGetString(Resource) && !Resource.IsEmpty())
			{
				Resources.AddUnique(Resource);
			}
		}
		return Resources;
	}
}

FString USessionChannelTool::GetToolDescription() const
{
	return TEXT("Coordinate with other LLM sessions sharing this editor. Other agents may be using it "
		"right now. Advisory only - nothing here blocks any tool call.");
}

TMap<FString, FBridgeSchemaProperty> USessionChannelTool::GetInputSchema() const
{
	TMap<FString, FBridgeSchemaProperty> Schema;

	FBridgeSchemaProperty Action = MakeProperty(TEXT("string"),
		TEXT("announce | list | broadcast | ask | answer | inbox | leave"));
	Action.bRequired = true;
	Action.Enum = {
		TEXT("announce"), TEXT("list"), TEXT("broadcast"),
		TEXT("ask"), TEXT("answer"), TEXT("inbox"), TEXT("leave")
	};
	Schema.Add(TEXT("action"), Action);

	Schema.Add(TEXT("as"), MakeProperty(TEXT("string"),
		TEXT("Your session name. Normally carried by params._session instead.")));
	Schema.Add(TEXT("status"), MakeProperty(TEXT("string"),
		TEXT("announce: one line on what you are doing")));

	FBridgeSchemaProperty Resources = MakeProperty(TEXT("array"),
		TEXT("announce: asset paths or names you depend on. Replaces the previous list."));
	Resources.ItemsType = TEXT("string");
	Schema.Add(TEXT("resources"), Resources);

	Schema.Add(TEXT("intent"), MakeProperty(TEXT("string"),
		TEXT("read | write | pie | build | editor-restart")));
	Schema.Add(TEXT("agent"), MakeProperty(TEXT("string"),
		TEXT("announce: harness name, e.g. claude-code or codex")));
	Schema.Add(TEXT("cwd"), MakeProperty(TEXT("string"),
		TEXT("announce: your working directory")));
	Schema.Add(TEXT("resource"), MakeProperty(TEXT("string"),
		TEXT("list: only sessions depending on this resource")));
	Schema.Add(TEXT("include_stale"), MakeProperty(TEXT("boolean"),
		TEXT("list: also show sessions silent for 15+ minutes")));
	Schema.Add(TEXT("message"), MakeProperty(TEXT("string"),
		TEXT("broadcast: what you want everyone to know")));
	Schema.Add(TEXT("tag"), MakeProperty(TEXT("string"),
		TEXT("broadcast: fyi | warning | request")));
	Schema.Add(TEXT("to"), MakeProperty(TEXT("string"),
		TEXT("ask: target session name, or 'all'")));
	Schema.Add(TEXT("question"), MakeProperty(TEXT("string"),
		TEXT("ask: what you want to know")));
	Schema.Add(TEXT("context"), MakeProperty(TEXT("string"),
		TEXT("ask: why you are asking")));
	Schema.Add(TEXT("ask_id"), MakeProperty(TEXT("string"),
		TEXT("answer: the ask being answered. inbox: only answers to this ask.")));
	Schema.Add(TEXT("answer"), MakeProperty(TEXT("string"),
		TEXT("answer: your reply in words")));
	Schema.Add(TEXT("decision"), MakeProperty(TEXT("string"),
		TEXT("answer: yes | no | wait, so the asker can branch without reading prose")));
	Schema.Add(TEXT("unread_only"), MakeProperty(TEXT("boolean"),
		TEXT("inbox: only messages you have not seen")));
	Schema.Add(TEXT("no_mark_read"), MakeProperty(TEXT("boolean"),
		TEXT("inbox: do not advance your read cursor")));
	Schema.Add(TEXT("since"), MakeProperty(TEXT("string"),
		TEXT("inbox: only messages newer than this ISO-8601 UTC timestamp")));
	Schema.Add(TEXT("reason"), MakeProperty(TEXT("string"),
		TEXT("leave: why you are leaving")));

	return Schema;
}

FBridgeToolResult USessionChannelTool::Execute(const TSharedPtr<FJsonObject>& Args, const FBridgeToolContext& Ctx)
{
	const FString Action = GetStringArgOrDefault(Args, TEXT("action"));
	if (Action.IsEmpty())
	{
		return FBridgeToolResult::Error(
			TEXT("session: 'action' is required (announce, list, broadcast, ask, answer, inbox, leave)"));
	}

	FBridgeSessionRegistry& Registry = FBridgeSessionRegistry::Get();

	// Identity comes from params._session, which both the CLI and the MCP server
	// always send. 'as' is only a fallback name for a caller that sent neither.
	const FString DeclaredAs = GetStringArgOrDefault(Args, TEXT("as"));
	const FString SelfId = FBridgeSessionRegistry::ResolveSessionId(Ctx);
	const FString SelfLabel = Ctx.SessionLabel.IsEmpty() ? DeclaredAs : Ctx.SessionLabel;

	if (Action == TEXT("announce"))
	{
		FSessionAnnouncement Announcement;
		Announcement.FallbackLabel = DeclaredAs;
		Announcement.Status = GetStringArgOrDefault(Args, TEXT("status"));
		Announcement.Intent = GetStringArgOrDefault(Args, TEXT("intent"));
		Announcement.Agent = GetStringArgOrDefault(Args, TEXT("agent"));
		Announcement.Cwd = GetStringArgOrDefault(Args, TEXT("cwd"));
		Announcement.Resources = ParseResources(Args, Announcement.bHasResources);

		const FSessionRecord Record = Registry.Announce(Ctx, Announcement);
		Registry.Flush();
		return FBridgeToolResult::Json(
			FBridgeSessionRegistry::RecordToJson(Record, FDateTime::UtcNow()));
	}

	if (Action == TEXT("list"))
	{
		const bool bIncludeStale = GetBoolArgOrDefault(Args, TEXT("include_stale"), false);
		const FString ResourceFilter = GetStringArgOrDefault(Args, TEXT("resource"));
		const FString IntentFilter = GetStringArgOrDefault(Args, TEXT("intent"));
		const FDateTime NowUtc = FDateTime::UtcNow();

		TArray<TSharedPtr<FJsonValue>> SessionValues;
		for (const FSessionRecord& Record : Registry.ListSessions(bIncludeStale))
		{
			if (!IntentFilter.IsEmpty() && Record.Intent != IntentFilter)
			{
				continue;
			}
			if (!ResourceFilter.IsEmpty() && !Record.Resources.Contains(ResourceFilter))
			{
				continue;
			}
			SessionValues.Add(MakeShared<FJsonValueObject>(
				FBridgeSessionRegistry::RecordToJson(Record, NowUtc)));
		}

		TSharedPtr<FJsonObject> Result = MakeShared<FJsonObject>();
		Result->SetArrayField(TEXT("sessions"), SessionValues);
		Result->SetStringField(TEXT("now_utc"), NowUtc.ToIso8601());
		Result->SetStringField(TEXT("you"), SelfId);
		return FBridgeToolResult::Json(Result);
	}

	if (Action == TEXT("broadcast"))
	{
		const FString Message = GetStringArgOrDefault(Args, TEXT("message"));
		if (Message.IsEmpty())
		{
			return FBridgeToolResult::Error(TEXT("session broadcast: 'message' is required"));
		}

		FSessionMessage Notice;
		Notice.Kind = TEXT("notice");
		Notice.From = SelfId;
		Notice.FromLabel = SelfLabel;
		Notice.Text = Message;
		Notice.Tag = GetStringArgOrDefault(Args, TEXT("tag"), TEXT("fyi"));

		const int32 Seq = Registry.Post(Notice);
		Registry.Flush();

		TSharedPtr<FJsonObject> Result = MakeShared<FJsonObject>();
		Result->SetNumberField(TEXT("seq"), Seq);
		Result->SetArrayField(TEXT("delivered_to"), AddressableIds(SelfId));
		return FBridgeToolResult::Json(Result);
	}

	if (Action == TEXT("ask"))
	{
		const FString Question = GetStringArgOrDefault(Args, TEXT("question"));
		if (Question.IsEmpty())
		{
			return FBridgeToolResult::Error(TEXT("session ask: 'question' is required"));
		}

		FString Requested = GetStringArgOrDefault(Args, TEXT("to"));
		if (Requested == TEXT("all"))
		{
			Requested.Reset();
		}
		const FString Resolved = ResolveTargetId(Requested);

		// An ask to a name nobody holds yet stays addressed to that name. Nobody
		// has it now, so delivered_to says so instead of leaving the caller
		// waiting -- but a session that registers under that name later still
		// receives it, because directed delivery is not cursor-gated.
		const FString TargetId = Requested.IsEmpty()
			? FString()
			: (Resolved.IsEmpty() ? Requested : Resolved);

		const FString AskId = Registry.OpenAsk(SelfId, SelfLabel, TargetId,
			Question, GetStringArgOrDefault(Args, TEXT("context")));
		Registry.Flush();

		TSharedPtr<FJsonObject> Result = MakeShared<FJsonObject>();
		Result->SetStringField(TEXT("ask_id"), AskId);
		if (Requested.IsEmpty())
		{
			Result->SetArrayField(TEXT("delivered_to"), AddressableIds(SelfId));
		}
		else if (!Resolved.IsEmpty())
		{
			TArray<TSharedPtr<FJsonValue>> DeliveredTo;
			DeliveredTo.Add(MakeShared<FJsonValueString>(Resolved));
			Result->SetArrayField(TEXT("delivered_to"), DeliveredTo);
		}
		else
		{
			Result->SetArrayField(TEXT("delivered_to"), TArray<TSharedPtr<FJsonValue>>());
			Result->SetStringField(TEXT("note"), FString::Printf(
				TEXT("No session named '%s' is registered yet, so nobody has this question "
				     "right now. It is held for that name and delivered if a session "
				     "registers under it. Check active_sessions for the name you meant, "
				     "and do not wait on an answer."),
				*Requested));
		}
		Result->SetArrayField(TEXT("active_sessions"), Registry.RosterJson(false));
		return FBridgeToolResult::Json(Result);
	}

	if (Action == TEXT("answer"))
	{
		const FString AskId = GetStringArgOrDefault(Args, TEXT("ask_id"));
		if (AskId.IsEmpty())
		{
			return FBridgeToolResult::Error(TEXT("session answer: 'ask_id' is required"));
		}

		const FString Answer = GetStringArgOrDefault(Args, TEXT("answer"));
		const FString Decision = GetStringArgOrDefault(Args, TEXT("decision"));
		if (!Registry.AnswerAsk(AskId, SelfId, SelfLabel, Answer, Decision))
		{
			return FBridgeToolResult::Error(FString::Printf(
				TEXT("session answer: ask '%s' is unknown or already answered. "
				     "An ask expires the moment it is answered; run 'session inbox' "
				     "to see which asks are still open for you."),
				*AskId));
		}
		Registry.Flush();

		TSharedPtr<FJsonObject> Result = MakeShared<FJsonObject>();
		Result->SetBoolField(TEXT("ok"), true);
		Result->SetStringField(TEXT("ask_id"), AskId);
		return FBridgeToolResult::Json(Result);
	}

	if (Action == TEXT("inbox"))
	{
		FDateTime SinceUtc;
		bool bHasSince = false;
		const FString Since = GetStringArgOrDefault(Args, TEXT("since"));
		if (!Since.IsEmpty())
		{
			if (!FDateTime::ParseIso8601(*Since, SinceUtc))
			{
				return FBridgeToolResult::Error(FString::Printf(
					TEXT("session inbox: 'since' must be an ISO-8601 UTC timestamp "
					     "like 2026-07-26T10:15:00Z, got '%s'"),
					*Since));
			}
			bHasSince = true;
		}

		const bool bUnreadOnly = GetBoolArgOrDefault(Args, TEXT("unread_only"), false);
		const bool bMarkRead = !GetBoolArgOrDefault(Args, TEXT("no_mark_read"), false);
		const FString AskIdFilter = GetStringArgOrDefault(Args, TEXT("ask_id"));

		TArray<TSharedPtr<FJsonValue>> MessageValues;
		TArray<TSharedPtr<FJsonValue>> AnswerValues;
		for (const FSessionMessage& Message : Registry.Inbox(SelfId, bUnreadOnly, bMarkRead))
		{
			if (bHasSince && Message.CreatedAtUtc <= SinceUtc)
			{
				continue;
			}

			const TSharedPtr<FJsonObject> Json = FBridgeSessionRegistry::MessageToJson(Message);
			MessageValues.Add(MakeShared<FJsonValueObject>(Json));

			const bool bIsAnswer = Message.Kind == TEXT("answer");
			if (bIsAnswer && (AskIdFilter.IsEmpty() || Message.AskId == AskIdFilter))
			{
				AnswerValues.Add(MakeShared<FJsonValueObject>(Json));
			}
		}

		TSharedPtr<FJsonObject> Result = MakeShared<FJsonObject>();
		Result->SetArrayField(TEXT("messages"), MessageValues);
		Result->SetArrayField(TEXT("answers"), AnswerValues);
		Result->SetArrayField(TEXT("silent"), RosterExcluding(SelfId, false));
		return FBridgeToolResult::Json(Result);
	}

	if (Action == TEXT("leave"))
	{
		const FString Reason = GetStringArgOrDefault(Args, TEXT("reason"));
		Registry.MarkEnded(SelfId, Reason.IsEmpty() ? FString(TEXT("left")) : Reason);
		Registry.Flush();

		TSharedPtr<FJsonObject> Result = MakeShared<FJsonObject>();
		Result->SetBoolField(TEXT("ok"), true);
		return FBridgeToolResult::Json(Result);
	}

	return FBridgeToolResult::Error(FString::Printf(
		TEXT("session: unknown action '%s'. Valid actions: announce, list, broadcast, "
		     "ask, answer, inbox, leave."),
		*Action));
}
