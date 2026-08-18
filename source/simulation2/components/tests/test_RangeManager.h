/* Copyright (C) 2026 Wildfire Games.
 * This file is part of 0 A.D.
 *
 * 0 A.D. is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 2 of the License, or
 * (at your option) any later version.
 *
 * 0 A.D. is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with 0 A.D.  If not, see <http://www.gnu.org/licenses/>.
 */

#include "lib/self_test.h"

#include "maths/Fixed.h"
#include "maths/FixedVector2D.h"
#include "maths/FixedVector3D.h"
#include "maths/Matrix3D.h"
#include "ps/CStr.h"
#include "ps/XML/Xeromyces.h"
#include "scriptinterface/Interface.h"
#include "simulation2/MessageTypes.h"
#include "simulation2/components/ICmpObstruction.h"
#include "simulation2/components/ICmpObstructionManager.h"
#include "simulation2/components/ICmpPosition.h"
#include "simulation2/components/ICmpRangeManager.h"
#include "simulation2/components/ICmpVision.h"
#include "simulation2/helpers/Player.h"
#include "simulation2/helpers/Position.h"
#include "simulation2/helpers/Los.h"
#include "simulation2/system/Component.h"
#include "simulation2/system/ComponentTest.h"
#include "simulation2/system/Entity.h"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <random>
#include <string>
#include <vector>

class MockVisionRgm : public ICmpVision
{
public:
	DEFAULT_MOCK_COMPONENT()

	entity_pos_t GetRange() const override { return entity_pos_t::FromInt(66); }
	bool GetRevealShore() const override { return false; }
};

class MockPositionRgm : public ICmpPosition
{
public:
	DEFAULT_MOCK_COMPONENT()

	void SetTurretParent(entity_id_t /*id*/, const CFixedVector3D& /*pos*/) override {}
	entity_id_t GetTurretParent() const override {return INVALID_ENTITY;}
	void UpdateTurretPosition() override {}
	std::set<entity_id_t>* GetTurrets() override { return nullptr; }
	bool IsInWorld() const override { return m_InWorld; }
	void MoveOutOfWorld() override { m_InWorld = false; }
	void MoveTo(entity_pos_t /*x*/, entity_pos_t /*z*/) override { }
	void MoveAndTurnTo(entity_pos_t /*x*/, entity_pos_t /*z*/, entity_angle_t /*a*/) override { }
	void JumpTo(entity_pos_t /*x*/, entity_pos_t /*z*/) override { }
	void SetHeightOffset(entity_pos_t dy) override { m_HeightOffset = dy; }
	entity_pos_t GetHeightOffset() const override { return m_HeightOffset; }
	void SetHeightFixed(entity_pos_t /*y*/) override { }
	entity_pos_t GetHeightFixed() const override { return entity_pos_t::Zero(); }
	entity_pos_t GetHeightAtFixed(entity_pos_t, entity_pos_t) const override { return entity_pos_t::Zero(); }
	bool IsHeightRelative() const override { return true; }
	void SetHeightRelative(bool /*relative*/) override { }
	bool CanFloat() const override { return false; }
	void SetFloating(bool /*flag*/) override { }
	void SetActorFloating(bool /*flag*/) override { }
	void SetActorAnchor(const CStr& /*anchor*/) override { }
	void SetConstructionProgress(fixed /*progress*/) override { }
	CFixedVector3D GetPosition() const override { return m_Pos; }
	CFixedVector2D GetPosition2D() const override { return CFixedVector2D(m_Pos.X, m_Pos.Z); }
	CFixedVector3D GetPreviousPosition() const override { return CFixedVector3D(); }
	CFixedVector2D GetPreviousPosition2D() const override { return CFixedVector2D(); }
	fixed GetTurnRate() const override { return fixed::Zero(); }
	void TurnTo(entity_angle_t /*y*/) override { }
	void SetYRotation(entity_angle_t /*y*/) override { }
	void SetXZRotation(entity_angle_t /*x*/, entity_angle_t /*z*/) override { }
	CFixedVector3D GetRotation() const override { return CFixedVector3D(); }
	fixed GetDistanceTravelled() const override { return fixed::Zero(); }
	void GetInterpolatedPosition2D(float /*frameOffset*/, float& x, float& z, float& rotY) const override { x = z = rotY = 0; }
	CMatrix3D GetInterpolatedTransform(float /*frameOffset*/) const override { return CMatrix3D(); }

	CFixedVector3D m_Pos;
	entity_pos_t m_HeightOffset = entity_pos_t::Zero();
	bool m_InWorld = true;
};

class MockObstructionRgm : public ICmpObstruction
{
public:
	DEFAULT_MOCK_COMPONENT();

	MockObstructionRgm(entity_pos_t s) : m_Size(s) {};

	ICmpObstructionManager::tag_t GetObstruction() const override { return {}; };
	bool GetObstructionSquare(ICmpObstructionManager::ObstructionSquare&) const override { return false; };
	bool GetPreviousObstructionSquare(ICmpObstructionManager::ObstructionSquare&) const override { return false; };
	entity_pos_t GetSize() const override { return m_Size; };
	CFixedVector2D GetStaticSize() const override { return {}; };
	EObstructionType GetObstructionType() const override { return {}; };
	void SetUnitClearance(const entity_pos_t&) override {};
	bool IsControlPersistent() const override { return {}; };
	bool CheckShorePlacement() const override { return {}; };
	EFoundationCheck CheckFoundation(const std::string&) const override { return {}; };
	EFoundationCheck CheckFoundation(const std::string& , bool) const override { return {}; };
	std::string CheckFoundation_wrapper(const std::string&, bool) const override { return {}; };
	bool CheckDuplicateFoundation() const override { return {}; };
	std::vector<entity_id_t> GetEntitiesByFlags(ICmpObstructionManager::flags_t) const override { return {}; };
	std::vector<entity_id_t> GetEntitiesBlockingMovement() const override { return {}; };
	std::vector<entity_id_t> GetEntitiesBlockingConstruction() const override { return {}; };
	std::vector<entity_id_t> GetEntitiesDeletedUponConstruction() const override { return {}; };
	void ResolveFoundationCollisions() const override {};
	void SetActive(bool) override {};
	void SetMovingFlag(bool) override {};
	void SetDisableBlockMovementPathfinding(bool, bool, int32_t) override {};
	bool GetBlockMovementFlag(bool) const override { return {}; };
	void SetControlGroup(entity_id_t) override {};
	entity_id_t GetControlGroup() const override { return {}; };
	void SetControlGroup2(entity_id_t) override {};
	entity_id_t GetControlGroup2() const override { return {}; };
private:
	entity_pos_t m_Size;
};

class TestCmpRangeManager : public CxxTest::TestSuite
{
	std::optional<CXeromycesEngine> xeromycesEngine;
public:
	void setUp()
	{
		xeromycesEngine.emplace();
	}

	void tearDown()
	{
		xeromycesEngine.reset();
	}

	// TODO It would be nice to call Verify() with the shore revealing system
	// but that means testing on an actual map, with water and land.

	void test_basic()
	{
		ComponentTestHelper test(*g_ScriptContext);

		ICmpRangeManager* rangeManager = test.Add<ICmpRangeManager>(CID_RangeManager, "", SYSTEM_ENTITY);

		MockVisionRgm vision;
		test.AddMock(100, IID_Vision, vision);

		MockPositionRgm position;
		test.AddMock(100, IID_Position, position);

		// This tests that the incremental computation produces the correct result
		// in various edge cases

		rangeManager->SetBounds(entity_pos_t::FromInt(0), entity_pos_t::FromInt(0), entity_pos_t::FromInt(512), entity_pos_t::FromInt(512));
		rangeManager->Verify();
		{ CMessageCreate msg(100); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();
		{ CMessageOwnershipChanged msg(100, -1, 1); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();
		{ CMessagePositionChanged msg(100, true, entity_pos_t::FromInt(247), entity_pos_t::FromDouble(257.95), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();
		{ CMessagePositionChanged msg(100, true, entity_pos_t::FromInt(247), entity_pos_t::FromInt(253), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();

		{ CMessagePositionChanged msg(100, true, entity_pos_t::FromInt(256), entity_pos_t::FromInt(256), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();

		{ CMessagePositionChanged msg(100, true, entity_pos_t::FromInt(256)+entity_pos_t::Epsilon(), entity_pos_t::FromInt(256), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();
		{ CMessagePositionChanged msg(100, true, entity_pos_t::FromInt(256)-entity_pos_t::Epsilon(), entity_pos_t::FromInt(256), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();
		{ CMessagePositionChanged msg(100, true, entity_pos_t::FromInt(256), entity_pos_t::FromInt(256)+entity_pos_t::Epsilon(), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();
		{ CMessagePositionChanged msg(100, true, entity_pos_t::FromInt(256), entity_pos_t::FromInt(256)-entity_pos_t::Epsilon(), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();

		{ CMessagePositionChanged msg(100, true, entity_pos_t::FromInt(383), entity_pos_t::FromInt(84), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();
		{ CMessagePositionChanged msg(100, true, entity_pos_t::FromInt(348), entity_pos_t::FromInt(83), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		rangeManager->Verify();

		std::mt19937 rng;
		for (size_t i = 0; i < 1024; ++i)
		{
			double x = std::uniform_real_distribution<double>(0.0, 512.0)(rng);
			double z = std::uniform_real_distribution<double>(0.0, 512.0)(rng);
			{ CMessagePositionChanged msg(100, true, entity_pos_t::FromDouble(x), entity_pos_t::FromDouble(z), entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
			rangeManager->Verify();
		}

		// Test OwnershipChange, GetEntitiesByPlayer, GetNonGaiaEntities
		{
			player_id_t previousOwner = -1;
			for (player_id_t newOwner = 0; newOwner < 8; ++newOwner)
			{
				CMessageOwnershipChanged msg(100, previousOwner, newOwner);
				rangeManager->HandleMessage(msg, false);

				for (player_id_t i = 0; i < 8; ++i)
					TS_ASSERT_EQUALS(rangeManager->GetEntitiesByPlayer(i).size(), i == newOwner ? 1 : 0);

				TS_ASSERT_EQUALS(rangeManager->GetNonGaiaEntities().size(), newOwner > 0 ? 1 : 0);
				previousOwner = newOwner;
			}
		}
	}

	void test_range_queries_distance_only()
	{
		ComponentTestHelper test(*g_ScriptContext);

		ICmpRangeManager* rangeManager = test.Add<ICmpRangeManager>(CID_RangeManager, "", SYSTEM_ENTITY);

		MockVisionRgm vision, vision2;
		MockPositionRgm position, position2;
		MockObstructionRgm obs(fixed::FromInt(2)), obs2(fixed::Zero());
		test.AddMock(100, IID_Vision, vision);
		test.AddMock(100, IID_Position, position);
		test.AddMock(100, IID_Obstruction, obs);

		test.AddMock(101, IID_Vision, vision2);
		test.AddMock(101, IID_Position, position2);
		test.AddMock(101, IID_Obstruction, obs2);

		rangeManager->SetBounds(entity_pos_t::FromInt(0), entity_pos_t::FromInt(0), entity_pos_t::FromInt(512), entity_pos_t::FromInt(512));
		rangeManager->Verify();
		{ CMessageCreate msg(100); rangeManager->HandleMessage(msg, false); }
		{ CMessageCreate msg(101); rangeManager->HandleMessage(msg, false); }

		// Don't set ownership for either entity - leave both as INVALID_PLAYER.
		// This bypasses the visibility check in TestEntityQuery, allowing us to test
		// the core distance calculation logic independently of the LOS system.

		auto move = [&rangeManager](entity_id_t ent, MockPositionRgm& pos, fixed x, fixed z) {
			pos.m_Pos = CFixedVector3D(x, fixed::Zero(), z);
			{ CMessagePositionChanged msg(ent, true, x, z, entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		};

		move(100, position, fixed::FromInt(10), fixed::FromInt(10));
		move(101, position2, fixed::FromInt(10), fixed::FromInt(20));

		// Query for owner -1 (INVALID_PLAYER) since both entities have no owner
		std::vector<entity_id_t> nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(0), fixed::FromInt(4), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{});
		nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(4), fixed::FromInt(50), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{101});

		move(101, position2, fixed::FromInt(10), fixed::FromInt(10));
		nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(0), fixed::FromInt(4), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{101});
		nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(4), fixed::FromInt(50), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{});

		move(101, position2, fixed::FromInt(10), fixed::FromInt(13));
		nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(0), fixed::FromInt(4), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{101});
		nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(4), fixed::FromInt(50), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{});

		move(101, position2, fixed::FromInt(10), fixed::FromInt(15));
		// In range thanks to self obstruction size.
		nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(0), fixed::FromInt(4), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{101});
		// In range thanks to target obstruction size.
		nearby = rangeManager->ExecuteQuery(101, fixed::FromInt(0), fixed::FromInt(4), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{100});

		// Trickier: min-range is closest-to-closest, but rotation may change the real distance.
		nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(2), fixed::FromInt(50), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{101});
		nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(5), fixed::FromInt(50), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{101});
		nearby = rangeManager->ExecuteQuery(100, fixed::FromInt(6), fixed::FromInt(50), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{});
		nearby = rangeManager->ExecuteQuery(101, fixed::FromInt(5), fixed::FromInt(50), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{100});
		nearby = rangeManager->ExecuteQuery(101, fixed::FromInt(6), fixed::FromInt(50), {-1}, 0, true);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{});

	}

	void test_range_queries_visibility_filtering()
	{
		ComponentTestHelper test(*g_ScriptContext);

		ICmpRangeManager* rangeManager = test.Add<ICmpRangeManager>(CID_RangeManager, "", SYSTEM_ENTITY);

		MockVisionRgm vision, vision2;
		MockPositionRgm position, position2;
		MockObstructionRgm obs(fixed::FromInt(2)), obs2(fixed::Zero());
		test.AddMock(100, IID_Vision, vision);
		test.AddMock(100, IID_Position, position);
		test.AddMock(100, IID_Obstruction, obs);

		test.AddMock(101, IID_Vision, vision2);
		test.AddMock(101, IID_Position, position2);
		test.AddMock(101, IID_Obstruction, obs2);

		rangeManager->SetBounds(entity_pos_t::FromInt(0), entity_pos_t::FromInt(0), entity_pos_t::FromInt(512), entity_pos_t::FromInt(512));
		rangeManager->Verify();
		{ CMessageCreate msg(100); rangeManager->HandleMessage(msg, false); }
		{ CMessageCreate msg(101); rangeManager->HandleMessage(msg, false); }

		// Set ownership for both entities so they have proper owners
		{ CMessageOwnershipChanged msg(100, -1, 1); rangeManager->HandleMessage(msg, false); }
		{ CMessageOwnershipChanged msg(101, -1, 1); rangeManager->HandleMessage(msg, false); }

		auto move = [&rangeManager](entity_id_t ent, MockPositionRgm& pos, fixed x, fixed z) {
			pos.m_Pos = CFixedVector3D(x, fixed::Zero(), z);
			{ CMessagePositionChanged msg(ent, true, x, z, entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		};

		move(100, position, fixed::FromInt(10), fixed::FromInt(10));
		move(101, position2, fixed::FromInt(10), fixed::FromInt(15));

		std::vector<int> owners;
		owners.push_back(1);

		// Test 1: With preferMirages = true, we should get the mirage entity when visible
		ICmpRangeManager::tag_t query = rangeManager->CreateActiveQuery(
			100,                                // source
			fixed::FromInt(0),                  // minRange
			fixed::FromInt(50),                 // maxRange
			owners,                             // owners
			0,                                  // requiredInterface
			rangeManager->GetEntityFlagMask("normal"),
			true,                               // accountForSize
			true                                // preferMirages = true
		);
		rangeManager->EnableActiveQuery(query);

		// With reveal map enabled, entity should be visible
		rangeManager->SetLosRevealWholeMap(1, true);
		{ CMessageUpdate msg(fixed::FromInt(1)); rangeManager->HandleMessage(msg, false); }

		std::vector<entity_id_t> nearby = rangeManager->ResetActiveQuery(query);
		// Should return either the real entity (101) or a mirage.
		// For this test, we just verify something is returned.
		TS_ASSERT_EQUALS(nearby.size(), 1);

		// Disable reveal map - entity should become hidden
		rangeManager->SetLosRevealWholeMap(1, false);
		{ CMessageUpdate msg(fixed::FromInt(1)); rangeManager->HandleMessage(msg, false); }

		nearby = rangeManager->ResetActiveQuery(query);
		// Should return empty (no visible entities)
		TS_ASSERT_EQUALS(nearby.size(), 0);

		// Re-enable reveal map
		rangeManager->SetLosRevealWholeMap(1, true);
		{ CMessageUpdate msg(fixed::FromInt(1)); rangeManager->HandleMessage(msg, false); }

		nearby = rangeManager->ResetActiveQuery(query);
		TS_ASSERT_EQUALS(nearby.size(), 1);

		// Test 2: With preferMirages = false (default), visibility filtering is disabled
		ICmpRangeManager::tag_t query2 = rangeManager->CreateActiveQuery(
			100,                                // source
			fixed::FromInt(0),                  // minRange
			fixed::FromInt(50),                 // maxRange
			owners,                             // owners
			0,                                  // requiredInterface
			rangeManager->GetEntityFlagMask("normal"),
			true,                               // accountForSize
			false                               // preferMirages = false
		);
		rangeManager->EnableActiveQuery(query2);

		// With preferMirages = false, entity should be returned even when hidden
		rangeManager->SetLosRevealWholeMap(1, false);
		{ CMessageUpdate msg(fixed::FromInt(1)); rangeManager->HandleMessage(msg, false); }

		nearby = rangeManager->ResetActiveQuery(query2);
		// Should return the real entity (101) even though hidden
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{101});

		// Clean up
		rangeManager->DestroyActiveQuery(query);
		rangeManager->DestroyActiveQuery(query2);
	}

	// Gaia has no slot in the per-player visibility mask, but the whole map is
	// revealed to it: a Gaia-owned source must not have its results filtered out.
	void test_range_queries_gaia_source_visibility()
	{
		ComponentTestHelper test(*g_ScriptContext);

		ICmpRangeManager* rangeManager = test.Add<ICmpRangeManager>(CID_RangeManager, "", SYSTEM_ENTITY);

		MockVisionRgm vision, vision2;
		MockPositionRgm position, position2;
		MockObstructionRgm obs(fixed::Zero()), obs2(fixed::Zero());
		test.AddMock(100, IID_Vision, vision);
		test.AddMock(100, IID_Position, position);
		test.AddMock(100, IID_Obstruction, obs);

		test.AddMock(101, IID_Vision, vision2);
		test.AddMock(101, IID_Position, position2);
		test.AddMock(101, IID_Obstruction, obs2);

		rangeManager->SetBounds(entity_pos_t::FromInt(0), entity_pos_t::FromInt(0), entity_pos_t::FromInt(512), entity_pos_t::FromInt(512));
		{ CMessageCreate msg(100); rangeManager->HandleMessage(msg, false); }
		{ CMessageCreate msg(101); rangeManager->HandleMessage(msg, false); }

		// A Gaia animal (100) hunting a player-owned unit (101).
		{ CMessageOwnershipChanged msg(100, -1, 0); rangeManager->HandleMessage(msg, false); }
		{ CMessageOwnershipChanged msg(101, -1, 1); rangeManager->HandleMessage(msg, false); }

		auto move = [&rangeManager](entity_id_t ent, MockPositionRgm& pos, fixed x, fixed z) {
			pos.m_Pos = CFixedVector3D(x, fixed::Zero(), z);
			{ CMessagePositionChanged msg(ent, true, x, z, entity_angle_t::Zero()); rangeManager->HandleMessage(msg, false); }
		};

		move(100, position, fixed::FromInt(10), fixed::FromInt(10));
		move(101, position2, fixed::FromInt(10), fixed::FromInt(15));

		const std::vector<int> owners{ 1 };

		// The query UnitAI uses to find attack targets: flat, mirage-aware.
		ICmpRangeManager::tag_t query = rangeManager->CreateActiveQuery(
			100, fixed::FromInt(0), fixed::FromInt(50), owners, 0,
			rangeManager->GetEntityFlagMask("normal"), true, true);
		rangeManager->EnableActiveQuery(query);

		{ CMessageUpdate msg(fixed::FromInt(1)); rangeManager->HandleMessage(msg, false); }
		std::vector<entity_id_t> nearby = rangeManager->ResetActiveQuery(query);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{101});

		// Same for the parabolic variant.
		ICmpRangeManager::tag_t parabolicQuery = rangeManager->CreateActiveParabolicQuery(
			100, fixed::FromInt(0), fixed::FromInt(50), fixed::FromInt(20), fixed::Zero(),
			owners, 0, rangeManager->GetEntityFlagMask("normal"), true);
		rangeManager->EnableActiveQuery(parabolicQuery);

		{ CMessageUpdate msg(fixed::FromInt(1)); rangeManager->HandleMessage(msg, false); }
		nearby = rangeManager->ResetActiveQuery(parabolicQuery);
		TS_ASSERT_EQUALS(nearby, std::vector<entity_id_t>{101});

		rangeManager->DestroyActiveQuery(query);
		rangeManager->DestroyActiveQuery(parabolicQuery);
	}

	void test_ParabolicRangeBasic()
	{
		ComponentTestHelper test(*g_ScriptContext);
		ICmpRangeManager* rangeManager = test.Add<ICmpRangeManager>(CID_RangeManager, "", SYSTEM_ENTITY);

		const entity_id_t source = 200;
		const entity_id_t target = 201;
		entity_pos_t range{fixed::FromInt(-3)};
		entity_pos_t yOrigin{fixed::FromInt(-20)};

		// Invalid range.
		TS_ASSERT_EQUALS(rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin), range);

		// No source ICmpPosition.
		range = fixed::FromInt(10);
		TS_ASSERT_EQUALS(rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin), NEVER_IN_RANGE);

		// No target ICmpPosition.
		MockPositionRgm cmpSourcePosition;
		test.AddMock(source, IID_Position, cmpSourcePosition);
		TS_ASSERT_EQUALS(rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin), NEVER_IN_RANGE);

		// Too much height difference.
		MockPositionRgm cmpTargetPosition;
		test.AddMock(target, IID_Position, cmpTargetPosition);
		TS_ASSERT_EQUALS(rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin), NEVER_IN_RANGE);

		// If no offset we get the range.
		range = fixed::FromInt(20);
		yOrigin = fixed::Zero();
		TS_ASSERT_EQUALS(rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin), range);
		TS_ASSERT_EQUALS(rangeManager->GetEffectiveParabolicRange(source, target, fixed::Zero(), yOrigin), fixed::Zero());

		// Normal case with yOrigin only (no terrain difference)
		yOrigin = fixed::FromInt(5);
		range = fixed::FromInt(10);
		TS_ASSERT_EQUALS(rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin), fixed::FromFloat(14.142136f));

		// Big range.
		range = fixed::FromInt(260);
		TS_ASSERT_EQUALS(rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin), fixed::FromFloat(264.952820f));
	}

	void test_ExploreCircle()
	{
		ComponentTestHelper test(*g_ScriptContext);

		ICmpRangeManager* cmp = test.Add<ICmpRangeManager>(CID_RangeManager, "", SYSTEM_ENTITY);

		// Set up map bounds (256x256)
		cmp->SetBounds(entity_pos_t::FromInt(0), entity_pos_t::FromInt(0), entity_pos_t::FromInt(256), entity_pos_t::FromInt(256));
		cmp->Verify();

		// Initially, no tiles should be explored
		TS_ASSERT_EQUALS(cmp->GetPercentMapExplored(1), 0);

		// Explore a circle at the center of the map with radius 50
		cmp->ExploreCircle(1, entity_pos_t::FromInt(128), entity_pos_t::FromInt(128), fixed::FromInt(50));

		// Check that some tiles were explored (not 0%, not 100%)
		u8 percentExplored = cmp->GetPercentMapExplored(1);
		TS_ASSERT(percentExplored > 0 && percentExplored < 100);

		// Test invalid player (should do nothing)
		cmp->ExploreCircle(0, entity_pos_t::FromInt(128), entity_pos_t::FromInt(128), fixed::FromInt(50));
		cmp->ExploreCircle(99, entity_pos_t::FromInt(128), entity_pos_t::FromInt(128), fixed::FromInt(50));
		// No crash, no changes - verify still works
		cmp->Verify();

		// Explore the entire map
		cmp->ExploreCircle(1, entity_pos_t::FromInt(128), entity_pos_t::FromInt(128), fixed::FromInt(1000));

		// The entire map should be explored
		TS_ASSERT_EQUALS(cmp->GetPercentMapExplored(1), 100);

		cmp->Verify();
	}

	void test_ParabolicRangeWithTerrain()
	{
		ComponentTestHelper test(*g_ScriptContext);
		ICmpRangeManager* rangeManager = test.Add<ICmpRangeManager>(CID_RangeManager, "", SYSTEM_ENTITY);

		const entity_id_t source{200};
		const entity_id_t target{201};

		MockPositionRgm sourcePos;
		MockPositionRgm targetPos;
		test.AddMock(source, IID_Position, sourcePos);
		test.AddMock(target, IID_Position, targetPos);

		const entity_pos_t range{fixed::FromInt(100)};
		const entity_pos_t yOrigin{fixed::Zero()};

		// Source on high ground (Y=10), target on low ground (Y=0)
		sourcePos.m_Pos = CFixedVector3D(fixed::Zero(), fixed::FromInt(10), fixed::Zero());
		targetPos.m_Pos = CFixedVector3D(fixed::Zero(), fixed::Zero(), fixed::FromInt(50));
		entity_pos_t effective = rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin);
		TS_ASSERT_DELTA(effective.ToFloat(), 109.5445f, 0.01f); // ~109.54

		// Source on low ground (Y=0), target on high ground (Y=10)
		sourcePos.m_Pos = CFixedVector3D(fixed::Zero(), fixed::Zero(), fixed::Zero());
		targetPos.m_Pos = CFixedVector3D(fixed::Zero(), fixed::FromInt(10), fixed::FromInt(50));
		effective = rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin);
		TS_ASSERT_DELTA(effective.ToFloat(), 89.4427f, 0.01f); // ~89.44

		// Source with height offset (Y=15), target on flat ground (Y=0), with yOrigin
		sourcePos.m_Pos = CFixedVector3D(fixed::Zero(), fixed::FromInt(15), fixed::Zero());
		targetPos.m_Pos = CFixedVector3D(fixed::Zero(), fixed::Zero(), fixed::FromInt(50));
		const entity_pos_t yOrigin2{fixed::FromInt(2)};
		effective = rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin2);
		TS_ASSERT_DELTA(effective.ToFloat(), 115.7583f, 0.01f); // ~115.76
	}

	void test_ParabolicRangeTargetTooHigh()
	{
		ComponentTestHelper test(*g_ScriptContext);
		ICmpRangeManager* rangeManager = test.Add<ICmpRangeManager>(CID_RangeManager, "", SYSTEM_ENTITY);

		const entity_id_t source{200};
		const entity_id_t target{201};

		MockPositionRgm sourcePos;
		MockPositionRgm targetPos;
		test.AddMock(source, IID_Position, sourcePos);
		test.AddMock(target, IID_Position, targetPos);

		// Source on flat ground (height=0), target very high (height=30)
		sourcePos.m_Pos = CFixedVector3D(fixed::Zero(), fixed::Zero(), fixed::Zero());
		targetPos.m_Pos = CFixedVector3D(fixed::Zero(), fixed::FromInt(30), fixed::Zero());

		const entity_pos_t range{fixed::FromInt(50)};
		const entity_pos_t yOrigin{fixed::Zero()};

		// heightDifference = 0 - 30 = -30, range/2 = 25
		// -30 < -25 → NEVER_IN_RANGE
		TS_ASSERT_EQUALS(rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin), NEVER_IN_RANGE);

		// Target at borderline height (25)
		targetPos.m_Pos = CFixedVector3D(fixed::Zero(), fixed::FromInt(25), fixed::Zero());

		const entity_pos_t effective = rangeManager->GetEffectiveParabolicRange(source, target, range, yOrigin);
		TS_ASSERT_DIFFERS(effective, NEVER_IN_RANGE);
		TS_ASSERT_EQUALS(effective, fixed::Zero());
	}
};
