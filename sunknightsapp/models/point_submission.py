from django.db import models
from django.db.models.signals import post_save, post_delete
import decimal

from .clan_user import ClanUser
from .points_info import PointsInfo
from .utility.children_save_finder import receiver_subclasses
from .diep_tank import DiepTank
from .diep_gamemode import DiepGamemode
from .guildfight import GuildFightParticipation, GuildFight
from django.dispatch import receiver
from ..backgroundTask.webhook_spam import post_new_guild_fight, post_new_user_point_submission, \
    post_new_manager_submission, \
    post_new_guildfight_points, post_guild_fight_results, post_new_OneOnOne_submission, post_new_submission, \
    post_submission_reverted,post_new_event_quest_submission


class BasicPointSubmission(models.Model):
    manager = models.ForeignKey(ClanUser, null=True, blank=True)
    accepted = models.BooleanField(default=False)
    decided = models.BooleanField(default=False)
    managerText = models.TextField(max_length=200, blank="", default="")
    date = models.DateTimeField(auto_now_add=True)
    pointsinfo = models.ForeignKey(PointsInfo, on_delete=models.CASCADE, db_index=True)
    points = models.DecimalField(decimal_places=2, max_digits=6, default=0, db_index=True)
    reverted = models.BooleanField(default=False)

    @property
    def daily_quest(self):
        from .daily_quest import DailyQuest
        from datetime import timedelta
        datesubtract = self.date - timedelta(days=1)
        quests = DailyQuest.objects.filter(date__range=(datesubtract, self.date)).order_by('-date')
        return quests.first()

    def __str__(self):
        return self.pointsinfo.user.discord_nickname + ": " + str(self.points)


class BasicUserPointSubmission(BasicPointSubmission):
    submitterText = models.TextField(max_length=200, default="")
    proof = models.CharField(max_length=200)
    gamemode = models.ForeignKey(DiepGamemode)
    tank = models.ForeignKey(DiepTank)
    score = models.PositiveIntegerField(default=0)


class PointsManagerAction(BasicPointSubmission):
    pass

class EventQuestSubmission(BasicPointSubmission):
    proof=models.CharField(max_length=200)
    submitterText = models.TextField(max_length=200, default="")


class OneOnOneFightSubmission(BasicPointSubmission):
    pointsinfoloser = models.ForeignKey(PointsInfo, related_name="loser")
    proof = models.CharField(max_length=200)
    pointsloser = models.DecimalField(decimal_places=2, max_digits=6, default=3, db_index=True)
    expected_outcome = models.DecimalField(decimal_places=2, max_digits=4, default=0.5, db_index=True)


class GuildFightPointsAction(PointsManagerAction):
    fightparticipation = models.OneToOneField(GuildFightParticipation, on_delete=models.CASCADE)


# https://web.archive.org/web/20120715042306/http://codeblogging.net/blogs/1/14
@receiver_subclasses(post_save, BasicPointSubmission, "BasicPoint_post_save")
def update_submission_points_on_save(sender, instance, created=False, **kwargs):
    """This is an extremly lazy (not efficient) method to always keep currentpoints up to date -
    regardless if a submission was accepted, unaccapted, created, deleted, whatever.
    A better method would be to just add/subtract points on specific actions"""
    reverted = instance.reverted

    decided = instance.decided
    accepted = instance.accepted
    updateCurrentPoints(instance)

    if reverted:
        post_submission_reverted(instance)
    elif isinstance(instance, BasicUserPointSubmission):
        post_new_user_point_submission(instance, accepted, decided)
    elif isinstance(instance, GuildFightPointsAction):
        post_new_guildfight_points(instance, accepted)
    elif isinstance(instance, PointsManagerAction):
        post_new_manager_submission(instance, accepted)
    elif isinstance(instance, OneOnOneFightSubmission):
        post_new_OneOnOne_submission(instance, accepted, decided)
    elif isinstance(instance,EventQuestSubmission):
        post_new_event_quest_submission(instance,accepted,decided)
    elif isinstance(instance, BasicPointSubmission):
        post_new_submission(instance, accepted,decided)



        # https://web.archive.org/web/20120715042306/http://codeblogging.net/blogs/1/14


@receiver_subclasses(post_delete, BasicPointSubmission, "BasicPoint_post_delete")
def update_submission_points_on_delete(sender, instance, **kwargs):
    """This is an extremly lazy (not efficient) method to always keep currentpoints up to date -
regardless if a submission was accepted, unaccapted, created, deleted, whatever.
A better method would be to just add/subtract points on specific actions"""
    updateCurrentPoints(instance)


@receiver(post_save, sender=GuildFight)
def create_fightsubmission(sender, instance=None, created=False, **kwargs):
    def handleFightParticipation(participation, points):
        try:
            objekt = GuildFightPointsAction.objects.get(fightparticipation=participation)
        except GuildFightPointsAction.DoesNotExist:
            GuildFightPointsAction.objects.create(fightparticipation=participation, manager=participation.fight.manager,
                                                  accepted=True, decided=True, pointsinfo=participation.user.pointsinfo,
                                                  points=points, managerText='Points from Guild fight')
        else:
            objekt.points = points
            objekt.managerText = 'Points from Guild fight'
            objekt.save()

    fight = instance

    if created:
        post_new_guild_fight(fight)

    elif not created:
        if fight.status != 1:  # fight finished

            post_guild_fight_results(fight)

            pointswinner = fight.pointswinner
            pointsloser = fight.pointsloser
            if fight.status == 4:  # draw
                pointswinner = pointsloser = fight.pointsloser

            # Grant points now
            for participant in fight.winnerparticipants:
                handleFightParticipation(participant, pointswinner)

            for participant in fight.loserparticipants:
                handleFightParticipation(participant, pointsloser)
        else:  # fight was changed from a finished condition to unfinished. Gonna revert points now
            GuildFightPointsAction.objects.filter(fightparticipation__fight=fight).delete()


def pointsupdater(pointsinfo):
    sumPoints = \
        decimal.Decimal(
            BasicPointSubmission.objects.filter(pointsinfo=pointsinfo, accepted=True, decided=True).aggregate(
                models.Sum('points'))['points__sum'] or 0.0)

    # get points from fights that were lost
    sumPoints += decimal.Decimal(
        OneOnOneFightSubmission.objects.filter(pointsinfoloser=pointsinfo, accepted=True, decided=True).aggregate(
            models.Sum('pointsloser'))['pointsloser__sum'] or 0.0)
    pointsinfo.currentpoints = sumPoints

    pointsinfo.save()


def updateCurrentPoints(submission):
    pointsinfo = submission.pointsinfo

    try:
        fight = OneOnOneFightSubmission.objects.get(id=submission.id)

        pointsinfoloser = fight.pointsinfoloser
        if fight.reverted:
            from .utility.little_things import ELO_K
            pointsinfo.elo = pointsinfo.elo - ELO_K * (1 - float(fight.expected_outcome))
            pointsinfoloser.elo = pointsinfoloser.elo - ELO_K * (0 - float(1-fight.expected_outcome))

        pointsupdater(pointsinfoloser)

    except OneOnOneFightSubmission.DoesNotExist:
        pass

    pointsupdater(pointsinfo)
