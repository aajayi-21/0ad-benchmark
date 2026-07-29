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
#include "maths/NUSpline.h"

#define TS_ASSERT_VEC_EQUALS(v, x, y, z) \
	TS_ASSERT_EQUALS(v.X, x); \
	TS_ASSERT_EQUALS(v.Y, y); \
	TS_ASSERT_EQUALS(v.Z, z);

class TestNUSpline : public CxxTest::TestSuite
{
public:
	void test_TNSAddNode()
	{
		TNSpline spline{};
		CFixedVector3D p0{fixed::FromInt(0), fixed::FromInt(0), fixed::FromInt(0)};
		CFixedVector3D r0{fixed::FromInt(1), fixed::FromInt(1), fixed::FromInt(1)};
		fixed d0 = fixed::FromInt(33);

		CFixedVector3D p1{fixed::FromInt(1), fixed::FromInt(1), fixed::FromInt(1)};
		CFixedVector3D r1{fixed::FromInt(2), fixed::FromInt(2), fixed::FromInt(2)};
		fixed d1 = fixed::FromInt(4);

		CFixedVector3D p2{fixed::FromInt(2), fixed::FromInt(2), fixed::FromInt(2)};
		CFixedVector3D r2{fixed::FromInt(3), fixed::FromInt(3), fixed::FromInt(3)};
		fixed d2 = fixed::FromInt(12);

		spline.AddNode(p0, r0, d0);
		spline.AddNode(p1, r1, d1);
		spline.AddNode(p2, r2, d2);

		TS_ASSERT_VEC_EQUALS(spline.GetPosition(0.f), 0, 0, 0);
		TS_ASSERT_VEC_EQUALS(spline.GetPosition(0.25f), 1, 1, 1);
		TS_ASSERT_VEC_EQUALS(spline.GetPosition(1.f), 2, 2, 2);

		TS_ASSERT_EQUALS(spline.GetMaxDistance(), fixed::FromInt(16));
	}

	void test_TNSInsertNode()
	{
		TNSpline spline{};
		CFixedVector3D p0{fixed::FromInt(0), fixed::FromInt(0), fixed::FromInt(0)};
		CFixedVector3D r0{fixed::FromInt(1), fixed::FromInt(1), fixed::FromInt(1)};
		fixed d0 = fixed::FromInt(4);

		CFixedVector3D p1{fixed::FromInt(1), fixed::FromInt(1), fixed::FromInt(1)};
		CFixedVector3D r1{fixed::FromInt(2), fixed::FromInt(2), fixed::FromInt(2)};
		fixed d1 = fixed::FromInt(33);

		CFixedVector3D p2{fixed::FromInt(2), fixed::FromInt(2), fixed::FromInt(2)};
		CFixedVector3D r2{fixed::FromInt(3), fixed::FromInt(3), fixed::FromInt(3)};
		fixed d2 = fixed::FromInt(12);

		spline.InsertNode(0, p1, r1, d1);
		spline.InsertNode(0, p0, r0, d0);
		spline.InsertNode(2, p2, r2, d2);

		TS_ASSERT_VEC_EQUALS(spline.GetPosition(0.f), 0, 0, 0);
		TS_ASSERT_VEC_EQUALS(spline.GetPosition(0.25f), 1, 1, 1);
		TS_ASSERT_VEC_EQUALS(spline.GetPosition(1.f), 2, 2, 2);

		TS_ASSERT_EQUALS(spline.GetMaxDistance(), fixed::FromInt(16));
	}

	void test_TNSRemoveNode()
	{
		TNSpline spline{};
		CFixedVector3D p0{fixed::FromInt(0), fixed::FromInt(0), fixed::FromInt(0)};
		CFixedVector3D r0{fixed::FromInt(1), fixed::FromInt(1), fixed::FromInt(1)};
		fixed d0 = fixed::FromInt(33);

		CFixedVector3D p1{fixed::FromInt(1), fixed::FromInt(1), fixed::FromInt(1)};
		CFixedVector3D r1{fixed::FromInt(2), fixed::FromInt(2), fixed::FromInt(2)};
		fixed d1 = fixed::FromInt(4);

		CFixedVector3D p2{fixed::FromInt(2), fixed::FromInt(2), fixed::FromInt(2)};
		CFixedVector3D r2{fixed::FromInt(3), fixed::FromInt(3), fixed::FromInt(3)};
		fixed d2 = fixed::FromInt(12);

		spline.AddNode(p0, r0, d0);
		spline.AddNode(p1, r1, d1);
		spline.AddNode(p2, r2, d2);

		spline.RemoveNode(2);

		TS_ASSERT_VEC_EQUALS(spline.GetPosition(0.f), 0, 0, 0);
		TS_ASSERT_VEC_EQUALS(spline.GetPosition(1.f), 1, 1, 1);

		TS_ASSERT_EQUALS(spline.GetMaxDistance(), fixed::FromInt(4));

		spline.RemoveNode(0);

		TS_ASSERT_EQUALS(spline.GetMaxDistance(), fixed::FromInt(0));

		spline.AddNode(p2, r2, d2);

		TS_ASSERT_VEC_EQUALS(spline.GetPosition(0.f), 1, 1, 1);
		TS_ASSERT_VEC_EQUALS(spline.GetPosition(1.f), 2, 2, 2);

		TS_ASSERT_EQUALS(spline.GetMaxDistance(), fixed::FromInt(12));
	}
};
