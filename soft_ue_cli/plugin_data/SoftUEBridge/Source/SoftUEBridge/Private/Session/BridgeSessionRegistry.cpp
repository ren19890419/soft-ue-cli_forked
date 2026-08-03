// Copyright soft-ue-expert. All Rights Reserved.

#include "Session/BridgeSessionRegistry.h"

#include "SoftUEBridgeModule.h"
#include "Tools/BridgeToolBase.h"
#include "HAL/PlatformFileManager.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/ScopeLock.h"
#include "Serialization/JsonSerializer.h"

namespace
{
	/** A message plus the exact command that answers it. */
	TSharedPtr<FJsonObject> MessageToNoticeJson(const FSessionMessage& Message)
	{
		TSharedPtr<FJsonObject> Json = FBridgeSessionRegistry::MessageToJson(Message);
		if (Message.Kind == TEXT("ask") && !Message.AskId.IsEmpty())
		{
			Json->SetStringField(TEXT("reply_with"), FString::Printf(
				TEXT("soft-ue-cli session answer --id %s --decision <yes|no|wait> --answer \"...\""),
				*Message.AskId));
		}
		return Json;
	}
}

FBridgeSessionRegistry& FBridgeSessionRegistry::Get()
{
	static FBridgeSessionRegistry Instance;
	static bool bRehydrated = false;
	if (!bRehydrated)
	{
		// Set first: Rehydrate must never recurse back into Get().
		bRehydrated = true;
		Instance.Rehydrate();
	}
	return Instance;
}

FString FBridgeSessionRegistry::ResolveSessionId(const FBridgeToolContext& Context)
{
	if (!Context.SessionId.IsEmpty())
	{
		return Context.SessionId;
	}
	// An undeclared caller still appears, it just is not addressable.
	return FString::Printf(TEXT("unknown:%s"),
		Context.OriginId.IsEmpty() ? TEXT("anonymous") : *Context.OriginId);
}

FSessionRecord& FBridgeSessionRegistry::Upsert(const FBridgeToolContext& Context)
{
	const FString Id = ResolveSessionId(Context);
	if (!Sessions.Contains(Id))
	{
		FSessionRecord NewRecord;
		NewRecord.Id = Id;
		NewRecord.StartedAtUtc = FDateTime::UtcNow();
		NewRecord.LastSeenUtc = NewRecord.StartedAtUtc;
		Sessions.Add(Id, MoveTemp(NewRecord));
		EvictStaleSessions();
	}

	// Re-find after the insert: Add can rehash and EvictStaleSessions can remove.
	FSessionRecord& Record = Sessions.FindChecked(Id);
	if (!Context.SessionLabel.IsEmpty())
	{
		Record.Label = Context.SessionLabel;
	}
	if (!Context.OriginId.IsEmpty())
	{
		Record.Origin = Context.OriginId;
	}
	if (!Context.ClientKind.IsEmpty())
	{
		Record.ClientKind = Context.ClientKind;
	}
	if (Context.ClientPid != 0)
	{
		Record.Pid = Context.ClientPid;
	}
	// FBridgeToolContext carries no confidence field; a declared session is
	// exactly one that sent a label, which is how the client derives it too.
	Record.Confidence = Context.SessionLabel.IsEmpty() ? TEXT("derived") : TEXT("declared");

	return Record;
}

FSessionRecord FBridgeSessionRegistry::Announce(const FBridgeToolContext& Context,
	const FSessionAnnouncement& Announcement)
{
	FScopeLock ScopeLock(&Lock);

	FSessionRecord& Record = Upsert(Context);
	if (Record.Label.IsEmpty() && !Announcement.FallbackLabel.IsEmpty())
	{
		Record.Label = Announcement.FallbackLabel;
	}
	if (!Announcement.Status.IsEmpty())
	{
		Record.Status = Announcement.Status;
	}
	if (!Announcement.Intent.IsEmpty())
	{
		Record.Intent = Announcement.Intent;
	}
	if (!Announcement.Agent.IsEmpty())
	{
		Record.Agent = Announcement.Agent;
	}
	if (!Announcement.Cwd.IsEmpty())
	{
		Record.Cwd = Announcement.Cwd;
	}
	if (Announcement.bHasResources)
	{
		// Declaring resources replaces the list: an announce is the whole current
		// picture, not an increment nobody can retract.
		Record.Resources = Announcement.Resources;
	}

	// Copied while the lock is held, so the caller never holds a map reference.
	return Record;
}

void FBridgeSessionRegistry::EvictStaleSessions()
{
	const FDateTime NowUtc = FDateTime::UtcNow();
	while (Sessions.Num() > MaxSessions)
	{
		FString OldestId;
		FDateTime OldestSeen = FDateTime::MaxValue();
		for (const TPair<FString, FSessionRecord>& Pair : Sessions)
		{
			const FString Grade = GradeLiveness(Pair.Value, NowUtc);
			if (Grade != TEXT("stale") && Grade != TEXT("ended"))
			{
				continue;
			}
			if (Pair.Value.LastSeenUtc < OldestSeen)
			{
				OldestId = Pair.Key;
				OldestSeen = Pair.Value.LastSeenUtc;
			}
		}
		if (OldestId.IsEmpty())
		{
			// Everything is live. The cap is advisory; never drop an active session.
			break;
		}
		Sessions.Remove(OldestId);
		PushedSeq.Remove(OldestId);
		ReadSeq.Remove(OldestId);
	}
}

void FBridgeSessionRegistry::Touch(const FBridgeToolContext& Context, const FString& ToolName)
{
	// A tool invoking another tool natively passes a default-constructed context
	// (CaptureScreenshotTool does exactly this for capture-viewport). That call has
	// no identity worth recording: registering it would create an "unknown:anonymous"
	// record that refreshes on every screenshot, never grades stale, never evicts,
	// and shows up in delivered_to as a recipient that will never read anything.
	// Guarding here rather than at the call site covers every future re-entrant tool.
	if (Context.SessionId.IsEmpty() && Context.OriginId.IsEmpty())
	{
		return;
	}

	FScopeLock ScopeLock(&Lock);

	FSessionRecord& Record = Upsert(Context);
	Record.LastSeenUtc = FDateTime::UtcNow();
	Record.LastTool = ToolName;
	// A session that just made a call is demonstrably back, whatever it said before.
	Record.bEnded = false;
	Record.StaleReason.Reset();
}

void FBridgeSessionRegistry::DrainInto(const FBridgeToolContext& Context, const TSharedPtr<FJsonObject>& ResultJson)
{
	if (!ResultJson.IsValid())
	{
		return;
	}

	// Same reasoning as Touch: a caller with no identity at all has no mailbox.
	// Without this, every identity-less caller shares one "unknown:anonymous"
	// cursor and the first one to call drains the others' notices.
	if (Context.SessionId.IsEmpty() && Context.OriginId.IsEmpty())
	{
		return;
	}

	const FString SessionId = ResolveSessionId(Context);
	TArray<TSharedPtr<FJsonValue>> Out;

	{
		FScopeLock ScopeLock(&Lock);

		// Broadcast: cursor-gated. Directed: delivered once, cursor-independent.
		// The cursor starts at 0, never at the current head: a question asked
		// before this session's first bridge call must still reach it.
		const int32 Cursor = PushedSeq.FindRef(SessionId);
		int32 HighestSeen = Cursor;
		for (FSessionMessage& Message : Messages)
		{
			HighestSeen = FMath::Max(HighestSeen, Message.Seq);
			if (Message.From == SessionId)
			{
				continue;   // never echo a session its own traffic
			}
			if (Message.bExpired)
			{
				continue;   // an answered ask is not worth pushing at anyone
			}
			const bool bDirected = !Message.To.IsEmpty() && Message.To == SessionId;
			if (bDirected)
			{
				if (Message.DeliveredTo.Contains(SessionId))
				{
					continue;
				}
				Message.DeliveredTo.Add(SessionId);
			}
			else if (!Message.To.IsEmpty() || Message.Seq <= Cursor)
			{
				continue;   // addressed to someone else, or already past the cursor
			}
			Out.Add(MakeShared<FJsonValueObject>(MessageToNoticeJson(Message)));
		}
		PushedSeq.Add(SessionId, HighestSeen);
	}

	if (Out.Num() > 0)
	{
		ResultJson->SetArrayField(TEXT("session_notices"), Out);
	}
}

void FBridgeSessionRegistry::ClaimResource(const FString& SessionId, const FString& Resource)
{
	if (SessionId.IsEmpty() || Resource.IsEmpty())
	{
		return;
	}

	FScopeLock ScopeLock(&Lock);
	if (FSessionRecord* Record = Sessions.Find(SessionId))
	{
		Record->Resources.AddUnique(Resource);
	}
}

void FBridgeSessionRegistry::ReleaseResource(const FString& SessionId, const FString& Resource)
{
	if (SessionId.IsEmpty() || Resource.IsEmpty())
	{
		return;
	}

	FScopeLock ScopeLock(&Lock);
	if (FSessionRecord* Record = Sessions.Find(SessionId))
	{
		Record->Resources.Remove(Resource);
	}
}

int32 FBridgeSessionRegistry::Post(const FSessionMessage& Message)
{
	FScopeLock ScopeLock(&Lock);

	FSessionMessage Stored = Message;
	Stored.Seq = NextSeq++;
	Stored.CreatedAtUtc = FDateTime::UtcNow();
	const int32 Seq = Stored.Seq;

	Messages.Add(MoveTemp(Stored));
	while (Messages.Num() > MaxMessages)
	{
		// Evict from the front only: cursors are sequence numbers, not indices.
		Messages.RemoveAt(0);
	}

	return Seq;
}

FString FBridgeSessionRegistry::OpenAsk(const FString& From, const FString& FromLabel, const FString& To,
	const FString& Question, const FString& Context)
{
	FScopeLock ScopeLock(&Lock);

	FSessionMessage Message;
	Message.Kind = TEXT("ask");
	Message.From = From;
	Message.FromLabel = FromLabel;
	Message.To = To;
	Message.Text = Context.IsEmpty()
		? Question
		: FString::Printf(TEXT("%s (context: %s)"), *Question, *Context);
	Message.AskId = FString::Printf(TEXT("a-%d"), NextAskId++);

	const FString AskId = Message.AskId;
	Post(Message);
	return AskId;
}

bool FBridgeSessionRegistry::AnswerAsk(const FString& AskId, const FString& From, const FString& FromLabel,
	const FString& Answer, const FString& Decision)
{
	FScopeLock ScopeLock(&Lock);

	FSessionMessage* Ask = nullptr;
	for (FSessionMessage& Candidate : Messages)
	{
		if (Candidate.Kind == TEXT("ask") && Candidate.AskId == AskId)
		{
			Ask = &Candidate;
			break;
		}
	}
	if (!Ask || Ask->bExpired)
	{
		return false;
	}

	FSessionMessage Reply;
	Reply.Kind = TEXT("answer");
	Reply.From = From;
	Reply.FromLabel = FromLabel;
	Reply.To = Ask->From;
	Reply.Text = Answer;
	Reply.Decision = Decision;
	Reply.AskId = AskId;

	// Expire before posting: Post appends to Messages, which invalidates Ask.
	Ask->bExpired = true;
	Ask = nullptr;

	Post(Reply);
	return true;
}

TArray<FSessionRecord> FBridgeSessionRegistry::ListSessions(bool bIncludeStale) const
{
	FScopeLock ScopeLock(&Lock);

	const FDateTime NowUtc = FDateTime::UtcNow();
	TArray<FSessionRecord> Out;
	for (const TPair<FString, FSessionRecord>& Pair : Sessions)
	{
		if (!bIncludeStale)
		{
			const FString Grade = GradeLiveness(Pair.Value, NowUtc);
			if (Grade == TEXT("stale") || Grade == TEXT("ended"))
			{
				continue;
			}
		}
		Out.Add(Pair.Value);
	}

	Out.Sort([](const FSessionRecord& A, const FSessionRecord& B)
	{
		return A.LastSeenUtc > B.LastSeenUtc;
	});
	return Out;
}

TArray<TSharedPtr<FJsonValue>> FBridgeSessionRegistry::RosterJson(bool bIncludeStale) const
{
	const FDateTime NowUtc = FDateTime::UtcNow();
	TArray<TSharedPtr<FJsonValue>> Out;
	for (const FSessionRecord& Record : ListSessions(bIncludeStale))
	{
		Out.Add(MakeShared<FJsonValueObject>(RecordToJson(Record, NowUtc)));
	}
	return Out;
}

TArray<TSharedPtr<FJsonValue>> FBridgeSessionRegistry::OtherLiveSessionsJson(const FString& ExcludeSessionId) const
{
	const FDateTime NowUtc = FDateTime::UtcNow();
	TArray<TSharedPtr<FJsonValue>> Out;
	// Stale and ended records are already filtered out: a destructive tool should
	// name who is actually there, not everyone who ever was.
	for (const FSessionRecord& Record : ListSessions(false))
	{
		if (Record.Id == ExcludeSessionId)
		{
			continue;
		}
		Out.Add(MakeShared<FJsonValueObject>(RecordToJson(Record, NowUtc)));
	}
	return Out;
}

TArray<FSessionMessage> FBridgeSessionRegistry::Inbox(const FString& SessionId, bool bUnreadOnly, bool bMarkRead)
{
	FScopeLock ScopeLock(&Lock);

	// The read cursor, which only this method advances. The push cursor is not
	// consulted and not touched: a notice that flashed past on stderr does not
	// mean the session read its inbox, and reading the inbox is not what
	// no_mark_read exists to avoid.
	const int32 Cursor = ReadSeq.FindRef(SessionId);
	int32 HighestSeen = Cursor;
	TArray<FSessionMessage> Out;

	for (const FSessionMessage& Message : Messages)
	{
		HighestSeen = FMath::Max(HighestSeen, Message.Seq);
		if (Message.From == SessionId)
		{
			continue;
		}
		if (!Message.To.IsEmpty() && Message.To != SessionId)
		{
			continue;
		}
		if (bUnreadOnly && Message.Seq <= Cursor)
		{
			continue;
		}
		Out.Add(Message);
	}

	if (bMarkRead)
	{
		ReadSeq.Add(SessionId, HighestSeen);
	}
	return Out;
}

void FBridgeSessionRegistry::MarkEnded(const FString& SessionId, const FString& Reason)
{
	FScopeLock ScopeLock(&Lock);

	if (FSessionRecord* Record = Sessions.Find(SessionId))
	{
		Record->bEnded = true;
		Record->StaleReason = Reason;
		Record->LastSeenUtc = FDateTime::UtcNow();
	}
}

FString FBridgeSessionRegistry::GradeLiveness(const FSessionRecord& Record, const FDateTime& NowUtc)
{
	if (Record.bEnded)
	{
		return TEXT("ended");
	}

	const double Seconds = (NowUtc - Record.LastSeenUtc).GetTotalSeconds();
	if (Seconds <= ActiveSeconds)
	{
		return TEXT("active");
	}
	if (Seconds <= IdleSeconds)
	{
		return TEXT("idle");
	}
	return TEXT("stale");
}

TSharedPtr<FJsonObject> FBridgeSessionRegistry::RecordToJson(const FSessionRecord& Record, const FDateTime& NowUtc)
{
	TSharedPtr<FJsonObject> Json = MakeShared<FJsonObject>();
	Json->SetStringField(TEXT("id"), Record.Id);
	Json->SetStringField(TEXT("label"), Record.Label);
	Json->SetStringField(TEXT("origin"), Record.Origin);
	Json->SetStringField(TEXT("client"), Record.ClientKind);
	Json->SetStringField(TEXT("confidence"), Record.Confidence);
	Json->SetStringField(TEXT("status"), Record.Status);
	Json->SetStringField(TEXT("intent"), Record.Intent);
	Json->SetStringField(TEXT("cwd"), Record.Cwd);
	if (!Record.Agent.IsEmpty())
	{
		Json->SetStringField(TEXT("agent"), Record.Agent);
	}

	TArray<TSharedPtr<FJsonValue>> ResourceValues;
	for (const FString& Resource : Record.Resources)
	{
		ResourceValues.Add(MakeShared<FJsonValueString>(Resource));
	}
	Json->SetArrayField(TEXT("resources"), ResourceValues);

	Json->SetStringField(TEXT("state"), GradeLiveness(Record, NowUtc));
	Json->SetNumberField(TEXT("last_seen_s"),
		FMath::FloorToDouble(FMath::Max(0.0, (NowUtc - Record.LastSeenUtc).GetTotalSeconds())));
	Json->SetNumberField(TEXT("started_s"),
		FMath::FloorToDouble(FMath::Max(0.0, (NowUtc - Record.StartedAtUtc).GetTotalSeconds())));
	Json->SetStringField(TEXT("last_tool"), Record.LastTool);
	if (!Record.StaleReason.IsEmpty())
	{
		Json->SetStringField(TEXT("stale_reason"), Record.StaleReason);
	}

	return Json;
}

TSharedPtr<FJsonObject> FBridgeSessionRegistry::MessageToJson(const FSessionMessage& Message)
{
	TSharedPtr<FJsonObject> Json = MakeShared<FJsonObject>();
	Json->SetNumberField(TEXT("seq"), Message.Seq);
	Json->SetStringField(TEXT("kind"), Message.Kind);
	Json->SetStringField(TEXT("from"), Message.From);
	Json->SetStringField(TEXT("from_label"), Message.FromLabel);
	Json->SetStringField(TEXT("to"), Message.To);
	Json->SetStringField(TEXT("text"), Message.Text);
	Json->SetStringField(TEXT("tag"), Message.Tag);
	Json->SetStringField(TEXT("created_utc"), Message.CreatedAtUtc.ToIso8601());
	Json->SetBoolField(TEXT("expired"), Message.bExpired);
	if (!Message.AskId.IsEmpty())
	{
		Json->SetStringField(TEXT("ask_id"), Message.AskId);
	}
	if (!Message.Decision.IsEmpty())
	{
		Json->SetStringField(TEXT("decision"), Message.Decision);
	}
	return Json;
}

void FBridgeSessionRegistry::Flush()
{
	FScopeLock ScopeLock(&Lock);

	const FString Dir = FPaths::Combine(FPaths::ProjectDir(), TEXT(".soft-ue-bridge"));
	IPlatformFile& PlatformFile = FPlatformFileManager::Get().GetPlatformFile();
	if (!PlatformFile.DirectoryExists(*Dir) && !PlatformFile.CreateDirectoryTree(*Dir))
	{
		UE_LOG(LogSoftUEBridge, Warning,
			TEXT("Session registry: cannot create %s, sessions.json not written"), *Dir);
		return;
	}

	const FDateTime NowUtc = FDateTime::UtcNow();
	TSharedPtr<FJsonObject> Root = MakeShared<FJsonObject>();
	Root->SetStringField(TEXT("written_utc"), NowUtc.ToIso8601());

	TArray<TSharedPtr<FJsonValue>> SessionValues;
	for (const TPair<FString, FSessionRecord>& Pair : Sessions)
	{
		TSharedPtr<FJsonObject> Json = RecordToJson(Pair.Value, NowUtc);
		// Absolute timestamps so a later process can age the record itself.
		Json->SetStringField(TEXT("last_seen_utc"), Pair.Value.LastSeenUtc.ToIso8601());
		Json->SetStringField(TEXT("started_utc"), Pair.Value.StartedAtUtc.ToIso8601());
		SessionValues.Add(MakeShared<FJsonValueObject>(Json));
	}
	Root->SetArrayField(TEXT("sessions"), SessionValues);

	// Open asks and the last shutdown warning only. The message history stays
	// in memory: this file is a status mirror, not a transcript.
	TArray<TSharedPtr<FJsonValue>> AskValues;
	const FSessionMessage* LatestShutdown = nullptr;
	for (const FSessionMessage& Message : Messages)
	{
		if (Message.Kind == TEXT("ask") && !Message.bExpired)
		{
			AskValues.Add(MakeShared<FJsonValueObject>(MessageToJson(Message)));
		}
		else if (Message.Kind == TEXT("shutdown_intent"))
		{
			LatestShutdown = &Message;
		}
	}
	Root->SetArrayField(TEXT("open_asks"), AskValues);
	if (LatestShutdown)
	{
		Root->SetObjectField(TEXT("shutdown_intent"), MessageToJson(*LatestShutdown));
	}

	FString JsonString;
	TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&JsonString);
	FJsonSerializer::Serialize(Root.ToSharedRef(), Writer);

	// ForceUTF8WithoutBOM, not the AutoDetect default: AutoDetect writes UTF-16LE
	// with a BOM the moment any field is non-ASCII, and a `cwd` with non-ASCII
	// characters is ordinary. This file exists to be read out of process by
	// Python, which would fail on UTF-16.
	const FString FilePath = FPaths::Combine(Dir, TEXT("sessions.json"));
	if (!FFileHelper::SaveStringToFile(JsonString, *FilePath,
		FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
	{
		UE_LOG(LogSoftUEBridge, Warning, TEXT("Session registry: failed to write %s"), *FilePath);
	}
}

void FBridgeSessionRegistry::Rehydrate()
{
	const FString FilePath = FPaths::Combine(
		FPaths::ProjectDir(), TEXT(".soft-ue-bridge"), TEXT("sessions.json"));

	FString JsonString;
	if (!FFileHelper::LoadFileToString(JsonString, *FilePath))
	{
		return;
	}

	TSharedPtr<FJsonObject> Root;
	const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonString);
	if (!FJsonSerializer::Deserialize(Reader, Root) || !Root.IsValid())
	{
		UE_LOG(LogSoftUEBridge, Warning, TEXT("Session registry: %s is not readable JSON"), *FilePath);
		return;
	}

	const TArray<TSharedPtr<FJsonValue>>* SessionValues = nullptr;
	if (!Root->TryGetArrayField(TEXT("sessions"), SessionValues) || !SessionValues)
	{
		return;
	}

	const FDateTime NowUtc = FDateTime::UtcNow();
	FScopeLock ScopeLock(&Lock);

	for (const TSharedPtr<FJsonValue>& Value : *SessionValues)
	{
		const TSharedPtr<FJsonObject>* Object = nullptr;
		if (!Value.IsValid() || !Value->TryGetObject(Object) || !Object)
		{
			continue;
		}

		FSessionRecord Record;
		(*Object)->TryGetStringField(TEXT("id"), Record.Id);
		if (Record.Id.IsEmpty())
		{
			continue;
		}

		FString LastSeenIso;
		if (!(*Object)->TryGetStringField(TEXT("last_seen_utc"), LastSeenIso) ||
			!FDateTime::ParseIso8601(*LastSeenIso, Record.LastSeenUtc))
		{
			continue;
		}
		if ((NowUtc - Record.LastSeenUtc).GetTotalMinutes() > 30.0)
		{
			continue;   // too old to tell anyone anything useful
		}

		FString StartedIso;
		if (!(*Object)->TryGetStringField(TEXT("started_utc"), StartedIso) ||
			!FDateTime::ParseIso8601(*StartedIso, Record.StartedAtUtc))
		{
			Record.StartedAtUtc = Record.LastSeenUtc;
		}

		(*Object)->TryGetStringField(TEXT("label"), Record.Label);
		(*Object)->TryGetStringField(TEXT("origin"), Record.Origin);
		(*Object)->TryGetStringField(TEXT("client"), Record.ClientKind);
		(*Object)->TryGetStringField(TEXT("confidence"), Record.Confidence);
		(*Object)->TryGetStringField(TEXT("status"), Record.Status);
		(*Object)->TryGetStringField(TEXT("intent"), Record.Intent);
		(*Object)->TryGetStringField(TEXT("agent"), Record.Agent);
		(*Object)->TryGetStringField(TEXT("cwd"), Record.Cwd);
		(*Object)->TryGetStringField(TEXT("last_tool"), Record.LastTool);

		const TArray<TSharedPtr<FJsonValue>>* ResourceValues = nullptr;
		if ((*Object)->TryGetArrayField(TEXT("resources"), ResourceValues) && ResourceValues)
		{
			for (const TSharedPtr<FJsonValue>& ResourceValue : *ResourceValues)
			{
				FString Resource;
				if (ResourceValue.IsValid() && ResourceValue->TryGetString(Resource))
				{
					Record.Resources.Add(Resource);
				}
			}
		}

		// Nothing survived the restart. Say so instead of implying they are live.
		Record.bEnded = true;
		Record.StaleReason = TEXT("editor_restarted");

		const FString RecordId = Record.Id;
		Sessions.Add(RecordId, MoveTemp(Record));
	}
}
