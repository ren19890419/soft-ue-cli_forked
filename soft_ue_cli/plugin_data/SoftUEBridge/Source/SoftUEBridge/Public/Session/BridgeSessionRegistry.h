// Copyright soft-ue-expert. All Rights Reserved.

#pragma once

#include "CoreMinimal.h"
#include "Dom/JsonObject.h"
#include "HAL/CriticalSection.h"

struct FBridgeToolContext;

/** One LLM session sharing this editor. */
struct FSessionRecord
{
	FString Id;
	FString Label;
	FString Origin;
	FString ClientKind;
	FString Confidence;
	int32 Pid = 0;
	FString Status;
	FString Intent;
	FString Agent;
	FString Cwd;
	TArray<FString> Resources;
	FDateTime StartedAtUtc;
	FDateTime LastSeenUtc;
	FString LastTool;
	bool bEnded = false;
	FString StaleReason;
};

/**
 * What an `announce` wants to change about its own record.
 *
 * Every field is optional: an empty string leaves the record's value alone, so a
 * caller announcing only a new status does not blank out its intent. Resources
 * are the exception and need bHasResources, because announcing an empty list is
 * a meaningful way to say "I depend on nothing now".
 */
struct FSessionAnnouncement
{
	/** Used only when the record has no label yet. */
	FString FallbackLabel;
	FString Status;
	FString Intent;
	FString Agent;
	FString Cwd;
	bool bHasResources = false;
	TArray<FString> Resources;
};

/** A broadcast, a directed question, an answer, or a shutdown warning. */
struct FSessionMessage
{
	int32 Seq = 0;
	FString Kind;      // notice | ask | answer | shutdown_intent
	FString From;
	FString FromLabel;
	FString To;        // empty means broadcast
	FString Text;
	FString Decision;  // yes | no | wait
	FString AskId;
	FString Tag;
	FDateTime CreatedAtUtc;
	bool bExpired = false;

	/**
	 * Sessions this directed message has been pushed to as a notice. In-memory only.
	 * Belongs to the push channel alone -- Inbox never writes it. See the two-cursor
	 * note on FBridgeSessionRegistry.
	 */
	TSet<FString> DeliveredTo;
};

/**
 * Advisory cross-session state. Never blocks a tool call.
 *
 * Lives in the runtime module on purpose: reloading the editor module via
 * RemoveToolsForModule destroys editor tool state, and a module reload is
 * exactly when sessions most need to see each other.
 *
 * Two independent delivery channels, two independent cursors:
 *
 *   push (PushedSeq + FSessionMessage::DeliveredTo) -- advanced only by DrainInto,
 *     which rides notices out on the next tool response the session happens to make.
 *   pull (ReadSeq) -- advanced only by Inbox, and only when it is told to mark read.
 *
 * They must not share a cursor. Sharing one is what makes `inbox --unread-only`
 * return nothing (any prior tool call already consumed the cursor) and what makes
 * `no_mark_read` a lie (the drain on that same request consumes it anyway).
 *
 * The cost of independence is that one directed message can surface twice: once
 * as a stderr notice and once in an explicit inbox read. That is deliberate. The
 * opposite failure -- a question silently missing from your inbox because a notice
 * scrolled past three commands ago -- is the one that loses questions.
 */
class SOFTUEBRIDGE_API FBridgeSessionRegistry
{
public:
	static FBridgeSessionRegistry& Get();

	/**
	 * Free heartbeat: called at the tool choke point before every Execute.
	 * Ignores a context with no identity at all -- that is a tool calling another
	 * tool natively, not a session.
	 */
	void Touch(const FBridgeToolContext& Context, const FString& ToolName);

	/**
	 * Adds "session_notices" to ResultJson when this session has undelivered messages.
	 * Ignores a context with no identity, for the same reason as Touch.
	 */
	void DrainInto(const FBridgeToolContext& Context, const TSharedPtr<FJsonObject>& ResultJson);

	void ClaimResource(const FString& SessionId, const FString& Resource);
	void ReleaseResource(const FString& SessionId, const FString& Resource);

	int32 Post(const FSessionMessage& Message);
	FString OpenAsk(const FString& From, const FString& FromLabel, const FString& To,
		const FString& Question, const FString& Context);
	bool AnswerAsk(const FString& AskId, const FString& From, const FString& FromLabel,
		const FString& Answer, const FString& Decision);

	TArray<FSessionRecord> ListSessions(bool bIncludeStale) const;
	TArray<TSharedPtr<FJsonValue>> RosterJson(bool bIncludeStale) const;

	/** Roster of other live sessions, for embedding in a destructive tool's own result. */
	TArray<TSharedPtr<FJsonValue>> OtherLiveSessionsJson(const FString& ExcludeSessionId) const;

	TArray<FSessionMessage> Inbox(const FString& SessionId, bool bUnreadOnly, bool bMarkRead);

	/**
	 * Registers the caller and applies its announcement, all under the lock.
	 * Returns a copy: no reference into the session map escapes this class, so a
	 * later insert that rehashes the map cannot invalidate anything a caller holds.
	 */
	FSessionRecord Announce(const FBridgeToolContext& Context, const FSessionAnnouncement& Announcement);
	void MarkEnded(const FString& SessionId, const FString& Reason);

	/** The id this context is filed under, synthesised when the caller declared none. */
	static FString ResolveSessionId(const FBridgeToolContext& Context);

	/** active | idle | stale | ended */
	static FString GradeLiveness(const FSessionRecord& Record, const FDateTime& NowUtc);
	static TSharedPtr<FJsonObject> RecordToJson(const FSessionRecord& Record, const FDateTime& NowUtc);
	static TSharedPtr<FJsonObject> MessageToJson(const FSessionMessage& Message);

	/** Mirror to <ProjectDir>/.soft-ue-bridge/sessions.json. Mutation only, never on heartbeat. */
	void Flush();
	void Rehydrate();

private:
	FBridgeSessionRegistry() = default;

	/** Finds or creates this caller's record. Caller holds Lock; the reference must not escape it. */
	FSessionRecord& Upsert(const FBridgeToolContext& Context);

	/** Drop the oldest stale/ended records once the table is over MaxSessions. Caller holds Lock. */
	void EvictStaleSessions();

	/** FCriticalSection is recursive, so a locked method may call another locked method. */
	mutable FCriticalSection Lock;
	TMap<FString, FSessionRecord> Sessions;
	TArray<FSessionMessage> Messages;
	/** Highest seq pushed to a session as a notice. Written only by DrainInto. */
	TMap<FString, int32> PushedSeq;
	/** Highest seq a session has read from its inbox. Written only by Inbox. */
	TMap<FString, int32> ReadSeq;
	int32 NextSeq = 1;
	int32 NextAskId = 1;

	static constexpr int32 MaxSessions = 64;
	static constexpr int32 MaxMessages = 200;
	static constexpr double ActiveSeconds = 90.0;
	static constexpr double IdleSeconds = 900.0;
};
