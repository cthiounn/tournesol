import random
from unittest.mock import patch

import numpy as np
import scipy.stats as ss
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from scipy.stats import truncnorm

from core.tests.factories.user import UserFactory
from core.utils.time import time_ago
from tournesol.models import ContributorRatingCriteriaScore, EntityCriteriaScore
from tournesol.models.poll import ALGORITHM_MEHESTAN
from tournesol.tests.factories.comparison import ComparisonCriteriaScoreFactory, ComparisonFactory
from tournesol.tests.factories.entity import VideoFactory
from tournesol.tests.factories.poll import CriteriaRankFactory, PollFactory


class FirstComparisonWithOnlineHeuristicMehestanTest(TransactionTestCase):
    def setUp(self):
        self.poll = PollFactory(algorithm=ALGORITHM_MEHESTAN)
        CriteriaRankFactory(poll=self.poll, criteria__name="criteria1")
        self.entities = VideoFactory.create_batch(2)
        (self.user1,) = UserFactory.create_batch(1)
        self.client = APIClient()

    @override_settings(UPDATE_MEHESTAN_SCORES_ON_COMPARISON=True)
    def test_insert_individual_scores_after_new_comparison_with_online_heuristic_update(
        self,
    ):

        contrib_before_insert = set(
            ContributorRatingCriteriaScore.objects.all().values_list()
        )
        self.assertEqual(ContributorRatingCriteriaScore.objects.count(), 0)
        self.assertEqual(
            EntityCriteriaScore.objects.filter(score_mode="default").count(), 0
        )

        self.client.force_authenticate(self.user1)
        resp = self.client.post(
            f"/users/me/comparisons/{self.poll.name}",
            data={
                "entity_a": {"uid": self.entities[0].uid},
                "entity_b": {"uid": self.entities[1].uid},
                "criteria_scores": [{"criteria": "criteria1", "score": 10}],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.content)

        # Individual scores related to the new comparison have been computed
        self.assertEqual(
            ContributorRatingCriteriaScore.objects.filter(
                contributor_rating__user=self.user1
            ).count(),
            2,
        )
        # The score related to the less prefered entity is negative
        user_score = ContributorRatingCriteriaScore.objects.get(
            contributor_rating__user=self.user1,
            contributor_rating__entity=self.entities[0],
            criteria="criteria1",
        )
        self.assertLess(user_score.score, 0)
        call_command("ml_train", "--unsave")


class SimpleComparisonWithOnlineHeuristicMehestanTest(TransactionTestCase):
    def setUp(self):
        self.poll = PollFactory(algorithm=ALGORITHM_MEHESTAN)
        CriteriaRankFactory(poll=self.poll, criteria__name="criteria1")

        self.entities = VideoFactory.create_batch(3)
        (self.user1,) = UserFactory.create_batch(1)

        comparison_user1 = ComparisonFactory(
            poll=self.poll,
            user=self.user1,
            entity_1=self.entities[0],
            entity_2=self.entities[1],
        )

        comparisons = list()
        comparisons.append((comparison_user1, 10))
        for (comparison, score) in comparisons:
            ComparisonCriteriaScoreFactory(
                comparison=comparison,
                criteria="criteria1",
                score=score,
            )

        self.client = APIClient()
        call_command("ml_train")

    @override_settings(UPDATE_MEHESTAN_SCORES_ON_COMPARISON=True)
    def test_insert_individual_scores_after_new_comparison_with_online_heuristic_update(
        self,
    ):
        self.assertEqual(ContributorRatingCriteriaScore.objects.count(), 2)
        self.assertEqual(
            EntityCriteriaScore.objects.filter(score_mode="default").count(), 2
        )

        self.client.force_authenticate(self.user1)
        resp = self.client.post(
            f"/users/me/comparisons/{self.poll.name}",
            data={
                "entity_a": {"uid": self.entities[1].uid},
                "entity_b": {"uid": self.entities[2].uid},
                "criteria_scores": [{"criteria": "criteria1", "score": 10}],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.content)

        call_command("ml_train", "--unsave")
        # Individual scores related to the new comparison have been computed
        self.assertEqual(
            ContributorRatingCriteriaScore.objects.filter(
                contributor_rating__user=self.user1
            ).count(),
            3,
        )
        # The score related to the less prefered entity is negative
        user_score = ContributorRatingCriteriaScore.objects.get(
            contributor_rating__user=self.user1,
            contributor_rating__entity=self.entities[0],
            criteria="criteria1",
        )
        self.assertLess(user_score.score, 0)

    @override_settings(UPDATE_MEHESTAN_SCORES_ON_COMPARISON=True)
    def test_update_individual_scores_after_new_comparison_with_online_heuristic_update(
        self,
    ):
        self.assertEqual(ContributorRatingCriteriaScore.objects.count(), 2)
        self.assertEqual(
            EntityCriteriaScore.objects.filter(score_mode="default").count(), 2
        )

        self.client.force_authenticate(self.user1)
        resp = self.client.put(
            f"/users/me/comparisons/{self.poll.name}/{self.entities[0].uid}/{self.entities[1].uid}/",
            data={
                "criteria_scores": [{"criteria": "criteria1", "score": -10}],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)

        call_command("ml_train", "--unsave")
        # Individual scores related to the update have been recomputed
        self.assertEqual(
            ContributorRatingCriteriaScore.objects.filter(
                contributor_rating__user=self.user1
            ).count(),
            2,
        )
        # The score related to the less prefered entity is negative
        user_score = ContributorRatingCriteriaScore.objects.get(
            contributor_rating__user=self.user1,
            contributor_rating__entity=self.entities[1],
            criteria="criteria1",
        )
        self.assertLess(user_score.score, 0)

    @override_settings(UPDATE_MEHESTAN_SCORES_ON_COMPARISON=True)
    def test_delete_individual_scores_after_new_comparison_with_online_heuristic_update(
        self,
    ):
        self.assertEqual(ContributorRatingCriteriaScore.objects.count(), 2)
        self.assertEqual(
            EntityCriteriaScore.objects.filter(score_mode="default").count(), 2
        )

        self.client.force_authenticate(self.user1)
        resp = self.client.delete(
            f"/users/me/comparisons/{self.poll.name}/{self.entities[0].uid}/{self.entities[1].uid}/",
        )

        self.assertEqual(resp.status_code, 204, resp.content)

        call_command("ml_train", "--unsave")

        self.assertEqual(
            ContributorRatingCriteriaScore.objects.filter(
                contributor_rating__user=self.user1
            ).count(),
            2,
        )
        # The score related is zero
        user_score = ContributorRatingCriteriaScore.objects.get(
            contributor_rating__user=self.user1,
            contributor_rating__entity=self.entities[0],
            criteria="criteria1",
        )
        self.assertAlmostEqual(user_score.score, 0)


class InsertHundredOfComparisonWithOnlineHeuristicMehestanTest(TransactionTestCase):
    def setUp(self):
        self.poll = PollFactory(algorithm=ALGORITHM_MEHESTAN)
        CriteriaRankFactory(poll=self.poll, criteria__name="criteria1")
        self.number_entities = 10
        self.entities = VideoFactory.create_batch(self.number_entities)
        (self.user1,) = UserFactory.create_batch(1)
        self.client = APIClient()

    @override_settings(UPDATE_MEHESTAN_SCORES_ON_COMPARISON=True)
    @patch("tournesol.throttling.BurstUserRateThrottle.get_rate")
    @patch("tournesol.throttling.SustainedUserRateThrottle.get_rate")
    def test_insert_individual_scores_after_new_comparison_with_online_heuristic_update(
        self, mock1, mock2
    ):
        mock1.return_value = "10000/min"
        mock2.return_value = "360000/hour"
        self.assertEqual(ContributorRatingCriteriaScore.objects.count(), 0)
        self.assertEqual(
            EntityCriteriaScore.objects.filter(score_mode="default").count(), 0
        )

        self.client.force_authenticate(self.user1)

        for i in range(self.number_entities):
            for j in range(i + 1, self.number_entities):
                print(i, j, self.entities[i])
                resp = self.client.post(
                    f"/users/me/comparisons/{self.poll.name}",
                    data={
                        "entity_a": {"uid": self.entities[i].uid},
                        "entity_b": {"uid": self.entities[j].uid},
                        "criteria_scores": [{"criteria": "criteria1", "score": 10}],
                    },
                    format="json",
                )

                self.assertEqual(resp.status_code, 201, resp.content)
                call_command("ml_train", "--unsave")

        # self.number_entities indiv score
        self.assertEqual(
            ContributorRatingCriteriaScore.objects.filter(
                contributor_rating__user=self.user1
            ).count(),
            self.number_entities,
        )
        self.assertEqual(
            ContributorRatingCriteriaScore.objects.count(), self.number_entities
        )
        self.assertEqual(
            EntityCriteriaScore.objects.filter(score_mode="default").count(),
            self.number_entities,
        )


class HundredOfComparisonWithOnlineHeuristicMehestanTest(TransactionTestCase):
    def setUp(self):
        self.poll = PollFactory(algorithm=ALGORITHM_MEHESTAN)
        CriteriaRankFactory(poll=self.poll, criteria__name="criteria1")
        self.number_entities = 10
        self.entities = VideoFactory.create_batch(self.number_entities)
        (self.user1,) = UserFactory.create_batch(1)

        comparisons = [
            (
                ComparisonFactory(
                    poll=self.poll,
                    user=self.user1,
                    entity_1=self.entities[i],
                    entity_2=self.entities[j],
                ),
                10,
            )
            for i in range(self.number_entities)
            for j in range(i + 1, self.number_entities)
        ]

        for (comparison, score) in comparisons:
            ComparisonCriteriaScoreFactory(
                comparison=comparison,
                criteria="criteria1",
                score=score,
            )

        self.client = APIClient()
        call_command("ml_train")

    @override_settings(UPDATE_MEHESTAN_SCORES_ON_COMPARISON=True)
    @patch("tournesol.throttling.BurstUserRateThrottle.get_rate")
    @patch("tournesol.throttling.SustainedUserRateThrottle.get_rate")
    def test_update_individual_scores_after_new_comparison_with_online_heuristic_update(
        self, mock1, mock2
    ):
        mock1.return_value = "10000/min"
        mock2.return_value = "360000/hour"
        self.assertEqual(
            ContributorRatingCriteriaScore.objects.count(), self.number_entities
        )
        self.assertEqual(
            EntityCriteriaScore.objects.filter(score_mode="default").count(),
            self.number_entities,
        )

        self.client.force_authenticate(self.user1)

        for i in range(self.number_entities):
            for j in range(i + 1, self.number_entities):
                print(i, j, self.entities[i])
                resp = self.client.put(
                    f"/users/me/comparisons/{self.poll.name}/{self.entities[i].uid}/{self.entities[j].uid}/",
                    data={
                        "criteria_scores": [{"criteria": "criteria1", "score": -10}],
                    },
                    format="json",
                )
                self.assertEqual(resp.status_code, 200, resp.content)
                call_command("ml_train", "--unsave")

        # self.number_entities indiv score
        self.assertEqual(
            ContributorRatingCriteriaScore.objects.filter(
                contributor_rating__user=self.user1
            ).count(),
            self.number_entities,
        )
        self.assertEqual(
            ContributorRatingCriteriaScore.objects.count(), self.number_entities
        )
        self.assertEqual(
            EntityCriteriaScore.objects.filter(score_mode="default").count(),
            self.number_entities,
        )


class RandomDozenOfComparisonWithOnlineHeuristicMehestanTest(TransactionTestCase):
    def setUp(self):
        self.poll = PollFactory(algorithm=ALGORITHM_MEHESTAN)
        CriteriaRankFactory(poll=self.poll, criteria__name="criteria1")
        self.number_entities = 20
        self.entities = VideoFactory.create_batch(self.number_entities)
        self.users = UserFactory.create_batch(1000)
        self.client = APIClient()
        self.list_of_tuple_index = [
            (i, j)
            for i in range(self.number_entities)
            for j in range(i + 1, self.number_entities)
        ]
        random.shuffle(self.list_of_tuple_index)
        print(self.list_of_tuple_index[:1])

    @override_settings(UPDATE_MEHESTAN_SCORES_ON_COMPARISON=True)
    @patch("tournesol.throttling.BurstUserRateThrottle.get_rate")
    @patch("tournesol.throttling.SustainedUserRateThrottle.get_rate")
    def test_insert_individual_scores_after_new_comparison_with_online_heuristic_update(
        self, mock1, mock2
    ):
        mock1.return_value = "10000/min"
        mock2.return_value = "360000/hour"
        self.assertEqual(ContributorRatingCriteriaScore.objects.count(), 0)
        self.assertEqual(
            EntityCriteriaScore.objects.filter(score_mode="default").count(), 0
        )
        for user in self.users:
            self.client.force_authenticate(user)
            # scale = 3.
            # range = 10
            # size = 1000

            # X = truncnorm(a=-range/scale, b=+range/scale, scale=scale).rvs(size=size)
            # X = X.round().astype(int)

            # x = np.arange(-10, 11)
            # prob = np.array(
            #     [
            #         0.13242928,
            #         0.12533694,
            #         0.1062586,
            #         0.08069342,
            #         0.05489084,
            #         0.03344625,
            #         0.01825486,
            #         0.00892463,
            #         0.00390822,
            #         0.00153299,
            #         0.00053861,
            #         0.00153299,
            #         0.00390822,
            #         0.00892463,
            #         0.01825486,
            #         0.03344625,
            #         0.05489084,
            #         0.08069342,
            #         0.1062586,
            #         0.12533694,
            #         0.13242928,
            #     ]
            # )
            # prob = prob / prob.sum()
            # X = np.random.choice(x, size=10000, p=prob)
            for indice, (i, j) in enumerate(self.list_of_tuple_index[:1]):
                resp = self.client.post(
                    f"/users/me/comparisons/{self.poll.name}",
                    data={
                        "entity_a": {"uid": self.entities[i].uid},
                        "entity_b": {"uid": self.entities[j].uid},
                        "criteria_scores": [
                            {"criteria": "criteria1", "score": random.randint(-10,10)}
                            # {"criteria": "criteria1", "score": X[indice]}
                        ],
                    },
                    format="json",
                )

                self.assertEqual(resp.status_code, 201, resp.content)
            call_command("ml_train", "--unsave", "--user_id", user.id)
            print("finish for user {}".format(user))
